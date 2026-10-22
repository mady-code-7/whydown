"""Database access layer for WhyDown.

Phase 3 (part A): storage layer for the dashboard.

Design rules:
* The database URL comes from the ``DATABASE_URL`` environment variable only
  (never hardcoded, never logged, never part of an error message - spec
  section 12).
* Every function opens a short-lived connection (no global pool) with
  ``connect_timeout=10``. The connection is closed again when the call is
  done, including on error, so a run can never leak one.
* Every statement is parameterised (%s); no string formatting ever touches
  user supplied SQL text (spec section 12).
* Only metadata is stored - never a response body (spec section 12).
* The .env file is loaded on the command line entry point only, never at
  import time, so that importing this module never reads a real .env file.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from functools import wraps
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

# libpq connect timeout in seconds (spec section 7: never let a database
# outage block a check run forever).
CONNECT_TIMEOUT = 10

# Maximum number of characters kept for the error detail of a check row, so a
# row cannot grow without bound.
MAX_ERROR_DETAIL_CHARS = 2000

# Result codes that count as "the site was up" for the uptime calculation:
# a slow but working site is still up (spec section 8.3).
UP_RESULTS = ("OK", "SLOW", "DNS_SLOW")

# The schema script (spec section 9).
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "init_db.sql"

# Matches a password in a libpq connection string, e.g. "password=hunter2".
_PASSWORD_PATTERN = re.compile(r"(password\s*=\s*)\S+", re.IGNORECASE)


class DatabaseError(RuntimeError):
    """Raised when a database operation fails.

    The message never contains the connection string: it holds a password and
    must not end up in a log, a dashboard or a stack trace (spec section 12).
    """


def _redact(text: str) -> str:
    """Remove the connection string and any password from a message."""
    if not text:
        return text
    url = os.getenv("DATABASE_URL", "")
    if url:
        text = text.replace(url, "<redacted>")
    return _PASSWORD_PATTERN.sub(r"\1<redacted>", text)


def _db_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    """Turn a driver error into a DatabaseError that hides the DSN."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except DatabaseError:
            raise
        except psycopg.Error as exc:
            raise DatabaseError(f"{func.__name__} failed: {_redact(str(exc))}") from exc

    return wrapper


def _connect() -> Any:
    """Open a short-lived connection built from ``DATABASE_URL``."""
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise DatabaseError("DATABASE_URL is not set")
    try:
        return psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT)
    except psycopg.Error as exc:
        # The original exception may embed the connection string, so it is
        # not chained onto the DatabaseError.
        raise DatabaseError(f"Database connection failed: {_redact(str(exc))}") from None


@contextmanager
def _connection(conn: Any | None = None) -> Iterator[Any]:
    """Yield a connection and close it again, also when the block fails.

    A connection passed in by the caller is used as is and left open.
    """
    if conn is not None:
        yield conn
        return

    session = _connect()
    try:
        yield session
    except Exception:
        with suppress(Exception):
            session.rollback()
        raise
    finally:
        with suppress(Exception):
            session.close()


@contextmanager
def _cursor(conn: Any) -> Iterator[Any]:
    """Yield a cursor that returns rows as dictionaries."""
    cur = conn.cursor(row_factory=dict_row)
    try:
        yield cur
    finally:
        with suppress(Exception):
            cur.close()


def _get(result: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a mapping or from an object attribute."""
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def _ms(value: float | None) -> int | None:
    """Round a millisecond timing to the integer the column expects."""
    if value is None:
        return None
    return round(value)


def _truncate(text: str | None, limit: int) -> str | None:
    """Truncate ``text`` to ``limit`` characters."""
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[:limit]


@_db_errors
def init_schema(conn: Any | None = None) -> None:
    """Create the tables and indexes if they do not exist yet.

    Idempotent: the script uses ``CREATE TABLE IF NOT EXISTS`` and
    ``CREATE INDEX IF NOT EXISTS`` (spec section 9), so it can be run again at
    any time - for example on every deployment.
    """
    try:
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatabaseError(f"Schema file {SCHEMA_PATH.name} could not be read: {exc}") from exc

    with _connection(conn) as session:
        with _cursor(session) as cur:
            cur.execute(sql)
        session.commit()


@_db_errors
def list_sites(conn: Any | None = None, enabled_only: bool = False) -> list[dict[str, Any]]:
    """Return the monitored sites, optionally only the enabled ones."""
    query = (
        "SELECT id, name, url, expected_status, keyword, slow_threshold_ms, "
        "enabled, created_at FROM sites"
    )
    params: tuple[Any, ...] = ()
    if enabled_only:
        query += " WHERE enabled = TRUE"
    query += " ORDER BY name"

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [dict(row) for row in rows]


@_db_errors
def add_site(
    name: str,
    url: str,
    expected_status: int = 200,
    keyword: str | None = None,
    slow_threshold_ms: int = 5000,
    enabled: bool = True,
    conn: Any | None = None,
) -> int:
    """Insert a site and return its new primary key."""
    query = """INSERT INTO sites
               (name, url, expected_status, keyword, slow_threshold_ms, enabled)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id"""
    params = (name, url, expected_status, keyword, slow_threshold_ms, enabled)

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        session.commit()

    if not row or row.get("id") is None:
        raise DatabaseError("The site could not be stored")
    return int(row["id"])


@_db_errors
def delete_site(site_id: int, conn: Any | None = None) -> bool:
    """Remove a site. Its check history goes with it (ON DELETE CASCADE)."""
    query = "DELETE FROM sites WHERE id = %s"

    with _connection(conn) as session:
        with _cursor(session) as cur:
            cur.execute(query, (site_id,))
            removed = cur.rowcount
        session.commit()
    return bool(removed and removed > 0)


@_db_errors
def save_check(site_id: int, result: Any, conn: Any | None = None) -> int:
    """Store the metadata of one check and return its new primary key.

    ``result`` is a ``checker.CheckResult`` (or an equivalent mapping). Only
    metadata is stored - the response body never reaches the database
    (spec section 12).
    """
    query = """INSERT INTO checks
               (site_id, status_code, result, failed_stage, dns_ms, tcp_ms, tls_ms,
                ttfb_ms, total_ms, resolved_ip, cert_expires_at, cert_expiring_soon,
                error_detail)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id"""
    params = (
        site_id,
        _get(result, "status_code"),
        _get(result, "result"),
        _get(result, "failed_stage"),
        _ms(_get(result, "dns_ms")),
        _ms(_get(result, "tcp_ms")),
        _ms(_get(result, "tls_ms")),
        _ms(_get(result, "ttfb_ms")),
        _ms(_get(result, "total_ms")),
        _get(result, "resolved_ip"),
        _get(result, "cert_expires_at"),
        bool(_get(result, "cert_expiring_soon", False)),
        _truncate(_get(result, "error_message"), MAX_ERROR_DETAIL_CHARS),
    )

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        session.commit()

    if not row or row.get("id") is None:
        raise DatabaseError("The check could not be stored")
    return int(row["id"])


@_db_errors
def get_recent_checks(site_id: int, limit: int = 50, conn: Any | None = None) -> list[dict[str, Any]]:
    """Return the most recent checks of a site, newest first."""
    query = (
        "SELECT id, site_id, checked_at, status_code, result, failed_stage, "
        "dns_ms, tcp_ms, tls_ms, ttfb_ms, total_ms, resolved_ip, cert_expires_at, "
        "cert_expiring_soon, error_detail FROM checks "
        "WHERE site_id = %s ORDER BY checked_at DESC LIMIT %s"
    )

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(query, (site_id, limit))
        rows = cur.fetchall()
    return [dict(row) for row in rows]


@_db_errors
def uptime_percent(site_id: int, hours: int = 24, conn: Any | None = None) -> float:
    """Percentage of the checks of the last ``hours`` hours that were up.

    OK, SLOW and DNS_SLOW count as up (spec section 8.3); every other code
    counts as down. A window without checks has no uptime and returns 0.0.
    """
    placeholders = ", ".join("%s" for _ in UP_RESULTS)
    query = (
        "SELECT COUNT(*) AS total, "
        f"COUNT(*) FILTER (WHERE result IN ({placeholders})) AS up "
        "FROM checks WHERE site_id = %s AND checked_at >= NOW() - (%s * INTERVAL '1 hour')"
    )
    params = tuple(UP_RESULTS) + (site_id, hours)

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(query, params)
        row = cur.fetchone()

    total = int(row["total"]) if row else 0
    up = int(row["up"]) if row else 0
    if total == 0:
        return 0.0
    return round(up / total * 100.0, 2)


@_db_errors
def site_summaries(conn: Any | None = None) -> list[dict[str, Any]]:
    """Return the state of every site for the dashboard and the API.

    The result holds one entry per site with the site data (id, name, url,
    enabled), the result and the time of its latest check and the number of
    checks and up checks of the last 24 hours and the last 7 days.

    The whole list is read with two queries - one for the sites with their
    latest check, one for the counts per site - so the number of queries never
    depends on the number of sites.

    OK, SLOW and DNS_SLOW count as up, exactly as in ``uptime_percent``
    (spec section 8.3). A site without any check has empty windows; the
    caller decides what an empty window means for the uptime.
    """
    sites_query = (
        "SELECT s.id, s.name, s.url, s.enabled, "
        "c.result AS latest_result, c.checked_at AS latest_checked_at "
        "FROM sites s "
        "LEFT JOIN LATERAL ("
        "SELECT result, checked_at FROM checks "
        "WHERE site_id = s.id ORDER BY checked_at DESC, id DESC LIMIT 1"
        ") c ON TRUE "
        "ORDER BY s.name"
    )

    placeholders = ", ".join("%s" for _ in UP_RESULTS)
    counts_query = (
        "SELECT site_id, "
        "COUNT(*) FILTER (WHERE checked_at >= NOW() - (%s * INTERVAL '1 hour')) "
        "AS checks_24h, "
        f"COUNT(*) FILTER (WHERE checked_at >= NOW() - (%s * INTERVAL '1 hour') "
        f"AND result IN ({placeholders})) AS up_24h, "
        "COUNT(*) FILTER (WHERE checked_at >= NOW() - (%s * INTERVAL '1 day')) "
        "AS checks_7d, "
        f"COUNT(*) FILTER (WHERE checked_at >= NOW() - (%s * INTERVAL '1 day') "
        f"AND result IN ({placeholders})) AS up_7d "
        "FROM checks WHERE checked_at >= NOW() - (%s * INTERVAL '1 day') "
        "GROUP BY site_id"
    )
    counts_params = (24, 24) + UP_RESULTS + (7, 7) + UP_RESULTS + (7,)

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(sites_query, ())
        site_rows = cur.fetchall()
        cur.execute(counts_query, counts_params)
        count_rows = cur.fetchall()

    counts = {_get(row, "site_id"): row for row in count_rows}

    summaries: list[dict[str, Any]] = []
    for row in site_rows:
        count = counts.get(_get(row, "id"), {})
        summaries.append({
            "id": _get(row, "id"),
            "name": _get(row, "name"),
            "url": _get(row, "url"),
            "enabled": bool(_get(row, "enabled")),
            "latest_result": _get(row, "latest_result"),
            "latest_checked_at": _get(row, "latest_checked_at"),
            "checks_24h": int(_get(count, "checks_24h", 0) or 0),
            "up_24h": int(_get(count, "up_24h", 0) or 0),
            "checks_7d": int(_get(count, "checks_7d", 0) or 0),
            "up_7d": int(_get(count, "up_7d", 0) or 0),
        })
    return summaries


@_db_errors
def list_incidents(limit: int = 50, conn: Any | None = None) -> list[dict[str, Any]]:
    """Return the incidents, newest first.

    The columns are the ones of the incidents table (spec section 9): id,
    site_id, started_at, ended_at, failure_type and consecutive_failures.
    An incident that is still open has no ``ended_at``.
    """
    query = (
        "SELECT id, site_id, started_at, ended_at, failure_type, "
        "consecutive_failures FROM incidents "
        "ORDER BY started_at DESC, id DESC LIMIT %s"
    )

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(query, (limit,))
        rows = cur.fetchall()
    return [dict(row) for row in rows]


@_db_errors
def get_open_incident(site_id: int, conn: Any | None = None) -> dict[str, Any] | None:
    """Return the incident that is still open for a site, or None.

    An incident is open while its ``ended_at`` is NULL (spec section 9).
    The newest one is returned when a site somehow has more than one open
    incident.
    """
    query = (
        "SELECT id, site_id, started_at, ended_at, failure_type, "
        "consecutive_failures FROM incidents "
        "WHERE site_id = %s AND ended_at IS NULL "
        "ORDER BY started_at DESC, id DESC LIMIT 1"
    )

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(query, (site_id,))
        row = cur.fetchone()

    if not row:
        return None
    return dict(row)


@_db_errors
def open_incident(
    site_id: int,
    failure_type: str,
    consecutive_failures: int = 1,
    conn: Any | None = None,
) -> int:
    """Open an incident for a site and return its new primary key.

    ``started_at`` is set by the database to the current time, so the
    incident always begins exactly when it is opened (spec section 9).
    """
    query = """INSERT INTO incidents
               (site_id, started_at, failure_type, consecutive_failures)
               VALUES (%s, NOW(), %s, %s)
               RETURNING id"""
    params = (site_id, failure_type, consecutive_failures)

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        session.commit()

    if not row or row.get("id") is None:
        raise DatabaseError("The incident could not be stored")
    return int(row["id"])


@_db_errors
def close_incident(incident_id: int, conn: Any | None = None) -> bool:
    """Close an incident by setting its ``ended_at`` and report success.

    Only an incident that is still open is closed, so a repeated close never
    changes the end time of an incident that has ended before.
    """
    query = "UPDATE incidents SET ended_at = NOW() WHERE id = %s AND ended_at IS NULL"

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(query, (incident_id,))
        closed = cur.rowcount
        session.commit()

    return bool(closed and closed > 0)


@_db_errors
def recent_results(site_id: int, limit: int = 20, conn: Any | None = None) -> list[str]:
    """Return the result codes of the latest checks of a site, newest first.

    The incident state machine uses this to count the consecutive failures
    (spec section 8.4). Only the codes are read - no other metadata and
    certainly no response body.
    """
    query = (
        "SELECT result FROM checks WHERE site_id = %s "
        "ORDER BY checked_at DESC, id DESC LIMIT %s"
    )

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(query, (site_id, limit))
        rows = cur.fetchall()

    return [str(row["result"]) for row in rows]


@_db_errors
def get_latest_incident(site_id: int, conn: Any | None = None) -> dict[str, Any] | None:
    """Return the newest incident of a site, or None.

    The alert of an incident needs the incident itself: its id, its start,
    its end and its failure type. The newest incident of a site is the one
    that was just opened or just closed - a site never has two open
    incidents at the same time (spec section 8.4).
    """
    query = (
        "SELECT id, site_id, started_at, ended_at, failure_type, "
        "consecutive_failures FROM incidents "
        "WHERE site_id = %s ORDER BY started_at DESC, id DESC LIMIT 1"
    )

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(query, (site_id,))
        row = cur.fetchone()

    if not row:
        return None
    return dict(row)


@_db_errors
def log_alert(
    site_id: int,
    incident_id: int | None,
    kind: str,
    channel: str = "telegram",
    success: bool = True,
    conn: Any | None = None,
) -> int:
    """Record one alert attempt in the alert log and return its new id.

    Every attempt is written down, successful or not, so the log shows what
    the project tried to tell the user (spec section 9). The log is also the
    memory of what was already sent: a second alert of the same kind for
    the same incident is never sent (spec section 8.4).

    ``sent_at`` is set by the database to the current time.
    """
    query = """INSERT INTO alert_log
               (incident_id, site_id, kind, sent_at, channel, success)
               VALUES (%s, %s, %s, NOW(), %s, %s)
               RETURNING id"""
    params = (incident_id, site_id, kind, channel, success)

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        session.commit()

    if not row or row.get("id") is None:
        raise DatabaseError("The alert attempt could not be stored")
    return int(row["id"])


@_db_errors
def alert_exists(incident_id: int, kind: str, conn: Any | None = None) -> bool:
    """Return True when an alert of this kind was already logged.

    The check is made against the alert log and not against a variable of
    the process, so a restart of the free Render instance never sends a
    second alert for the same incident (spec section 8.4, test case T14).
    """
    query = "SELECT 1 FROM alert_log WHERE incident_id = %s AND kind = %s LIMIT 1"

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(query, (incident_id, kind))
        row = cur.fetchone()

    return row is not None


@_db_errors
def log_cert_alert(
    site_id: int,
    cert_expires_at: Any,
    threshold_days: int,
    kind: str = "CERT_WARNING",
    channel: str = "telegram",
    success: bool = True,
    conn: Any | None = None,
) -> int:
    """Record one certificate-expiry warning in the alert log.

    The row holds the expiry date of the certificate and the threshold that
    was crossed. Together with the site they are the memory of what was
    already warned about, so a threshold fires once per certificate (spec
    section 8.4, test case T09). A certificate that was renewed has a
    different expiry date and therefore starts the cycle again.

    Every attempt is written down, successful or not, so the log shows what
    the project tried to tell the user (spec section 9).
    """
    query = """INSERT INTO alert_log
               (incident_id, site_id, kind, sent_at, channel, success,
                cert_expires_at, cert_threshold_days)
               VALUES (NULL, %s, %s, NOW(), %s, %s, %s, %s)
               RETURNING id"""
    params = (
        site_id,
        kind,
        channel,
        success,
        cert_expires_at,
        int(threshold_days),
    )

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(query, params)
        row = cur.fetchone()
        session.commit()

    if not row or row.get("id") is None:
        raise DatabaseError("The certificate warning could not be stored")
    return int(row["id"])


@_db_errors
def cert_alert_exists(
    site_id: int,
    cert_expires_at: Any,
    threshold_days: int,
    kind: str = "CERT_WARNING",
    conn: Any | None = None,
) -> bool:
    """Return True when this certificate and this threshold were warned about.

    The check is made against the alert log and not against a variable of
    the process, so a restart of the free Render instance never sends a
    second warning for the same threshold of the same certificate (spec
    section 8.4, test case T09).
    """
    query = (
        "SELECT 1 FROM alert_log WHERE site_id = %s AND kind = %s "
        "AND cert_expires_at = %s AND cert_threshold_days = %s LIMIT 1"
    )
    params = (site_id, kind, cert_expires_at, int(threshold_days))

    with _connection(conn) as session, _cursor(session) as cur:
        cur.execute(query, params)
        row = cur.fetchone()

    return row is not None


@_db_errors
def purge_old_checks(days: int = 30, conn: Any | None = None) -> int:
    """Delete check rows older than ``days`` days and return how many went.

    Keeps the free database small (spec section 7: table growth over time).
    """
    query = "DELETE FROM checks WHERE checked_at < NOW() - (%s * INTERVAL '1 day')"

    with _connection(conn) as session:
        with _cursor(session) as cur:
            cur.execute(query, (days,))
            deleted = cur.rowcount
        session.commit()
    return int(deleted or 0)


# --------------------------------------------------------------------------
# Command line entry points
# --------------------------------------------------------------------------

USAGE = """usage: python -m app.db COMMAND

commands:
  init      Create the database schema if it does not exist yet.
  smoke     Write a test site and a fake check, read them back, print the
            result and delete the test data again.
"""


def main(argv: list[str] | None = None) -> int:
    """Entry point of ``python -m app.db``.

    Returns 0 on success and a non-zero code on failure. Neither a success
    nor a failure ever prints the DATABASE_URL.
    """
    # Load the project .env file once, before any command is dispatched, so
    # DATABASE_URL is in the environment when the command reads it. This is
    # done on the command line entry point only - never at import time - so
    # that importing this module (during the test run, for example) never
    # reads a real .env file.
    load_dotenv()

    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        print(USAGE)
        return 1

    command = argv[0]
    if command in ("-h", "--help"):
        print(USAGE)
        return 0

    if command == "init":
        try:
            init_schema()
        except DatabaseError as exc:
            print(f"init failed: {exc}", file=sys.stderr)
            return 1
        print("Schema is up to date.")
        return 0

    if command == "smoke":
        try:
            run_smoke()
        except DatabaseError as exc:
            print(f"smoke failed: {exc}", file=sys.stderr)
            return 1
        print("Smoke test finished, no test data was left behind.")
        return 0

    print(f"unknown command {command!r}", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


def run_smoke() -> None:
    """Write a test site and a fake check, read them back, clean up.

    The test data is removed again even when a step in between fails.
    """
    from .checker import CheckResult

    site_id: int | None = None
    try:
        site_id = add_site(
            name="smoke-test",
            url="https://smoke.invalid/page",
            expected_status=200,
            keyword="login",
            slow_threshold_ms=1000,
        )

        result = CheckResult(
            dns_success=True,
            tcp_success=True,
            tls_success=True,
            http_success=True,
            status_code=200,
            response_size=12,
            keyword_found=True,
            resolved_ip="127.0.0.1",
            dns_ms=1.2,
            tcp_ms=2.4,
            tls_ms=3.1,
            ttfb_ms=4.5,
            total_ms=12.4,
            result="OK",
        )
        check_id = save_check(site_id, result)

        sites = list_sites()
        recent = get_recent_checks(site_id, limit=5)
        uptime = uptime_percent(site_id, hours=24)

        print(f"site    id={site_id} name='smoke-test' url=https://smoke.invalid/page")
        print(f"check   id={check_id} result={result.result} total_ms={_ms(result.total_ms)}")
        print(f"read    {len(sites)} site(s), {len(recent)} recent check(s), uptime_24h={uptime}%")
    finally:
        if site_id is not None:
            delete_site(site_id)


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
