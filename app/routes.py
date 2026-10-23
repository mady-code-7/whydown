"""Flask routes for WhyDown application."""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from flask import (
    Flask,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import auth, db
from .checker import parse_url
from .config import CRON_SECRET_MIN_LENGTH, config, load_cron_secret
from .runner import run_all_checks

logger = logging.getLogger(__name__)

# Header a scheduler may use to send the token instead of a query parameter.
CRON_TOKEN_HEADER = "X-Cron-Token"

# The original runner function. It is kept so that a replaced name on this
# module can be told apart from an untouched one.
_ORIGINAL_RUN_ALL_CHECKS = run_all_checks

# Default and maximum number of rows the read-only API returns.
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

# The fields of a site that the admin area can change (spec section 9).
SITE_FIELDS = (
    "name",
    "url",
    "expected_status",
    "keyword",
    "slow_threshold_ms",
    "enabled",
)

# The default values of a new site (spec section 9).
DEFAULT_EXPECTED_STATUS = 200
DEFAULT_SLOW_THRESHOLD_MS = 5000

# The longest name, URL and keyword a site may have.
MAX_NAME_LENGTH = 200
MAX_URL_LENGTH = 2000
MAX_KEYWORD_LENGTH = 200


def configured_secret() -> str:
    """Return the configured scheduler secret, or "" when there is none.

    The environment is read on every request so that the secret can be
    rotated without a restart; an app level ``CRON_SECRET`` overrides it.

    A value shorter than ``CRON_SECRET_MIN_LENGTH`` counts as "not
    configured": a weak secret must never open the endpoint. The value is
    never logged and never put into a response (spec section 12).
    """
    candidate = current_app.config.get("CRON_SECRET") or load_cron_secret()
    candidate = candidate.strip()
    if len(candidate) < CRON_SECRET_MIN_LENGTH:
        return ""
    return candidate


def provided_token() -> str:
    """Return the token sent with the request, or "" when there is none.

    The token may arrive as the ``token`` query parameter (cron-job.org uses
    a GET request), in the ``X-Cron-Token`` header or in the body of a POST
    request.
    """
    return (
        request.args.get("token")
        or request.headers.get(CRON_TOKEN_HEADER)
        or request.form.get("token")
        or ""
    )


def require_cron_token(func: Callable) -> Callable:
    """Protect a route with the scheduler token.

    The token is compared with the configured secret using
    ``hmac.compare_digest``. Neither the secret nor the token is ever
    logged or put into a response body (spec section 12).

    * no secret configured -> 503, the endpoint fails closed;
    * no token sent         -> 401;
    * wrong token           -> 403.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        secret = configured_secret()
        if not secret:
            # Fail closed. The body does not name the setting and does not
            # contain its value.
            return jsonify({"error": "Check scheduling is not available."}), 503

        token = provided_token()
        if not token:
            return jsonify({"error": "Token required"}), 401

        if not hmac.compare_digest(token.encode("utf-8"), secret.encode("utf-8")):
            return jsonify({"error": "Invalid token"}), 403

        return func(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------
# Read-only JSON API (spec section 11)
# --------------------------------------------------------------------------

def _iso_utc(value: Any) -> Any:
    """Return a datetime as an ISO 8601 string in UTC.

    A datetime without a time zone is read as UTC. Every other value is
    returned unchanged.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return value


def _parse_limit() -> int | None:
    """Return the limit of the request, or None when it is not usable.

    The default is 50 rows, the maximum is 500. A limit that is not a
    positive integer is rejected (the caller answers 400).
    """
    raw = request.args.get("limit")
    if raw is None:
        return DEFAULT_LIMIT
    raw = raw.strip()
    # isdecimal() and not isdigit(): a digit like "²" is not a number that
    # int() can read, and it must not become an error page.
    if not raw.isdecimal():
        return None
    limit = int(raw)
    if limit <= 0:
        return None
    return min(limit, MAX_LIMIT)


def _uptime(checks: int, up: int) -> float | None:
    """Return the uptime in percent, or None when there was no check.

    A window without a check has no uptime at all, so the value is None and
    not 0.0 - a site that was never checked is not the same as a site that
    was down.
    """
    if not checks:
        return None
    return round(up / checks * 100.0, 2)


@contextmanager
def _api_connection() -> Iterator[Any]:
    """Yield one database connection for a request and close it again.

    The connection is used for every query of the request and is closed in
    the ``finally`` block, also when a query fails.
    """
    conn = db._connect()
    try:
        yield conn
    finally:
        with suppress(Exception):
            conn.close()


def _api_error(message: str, status: int) -> tuple[Any, int]:
    """Return a generic JSON error for the API."""
    return jsonify({"error": message}), status


def _api_sites() -> tuple[Any, int]:
    """Return every site with its current state (spec section 11)."""
    try:
        with _api_connection() as conn:
            rows = db.site_summaries(conn=conn)
    except Exception as exc:  # noqa: BLE001 - any database failure is a 503
        # Only the type of the error is logged: the message of a database
        # error may contain the connection string (spec section 12).
        logger.error("Reading the site list failed (%s)", type(exc).__name__)
        return _api_error("The site list is not available.", 503)

    sites = []
    for row in rows:
        checks_24h = int(row.get("checks_24h") or 0)
        up_24h = int(row.get("up_24h") or 0)
        checks_7d = int(row.get("checks_7d") or 0)
        up_7d = int(row.get("up_7d") or 0)
        sites.append({
            "id": row.get("id"),
            "name": row.get("name"),
            "url": row.get("url"),
            "enabled": bool(row.get("enabled")),
            "latest_result": row.get("latest_result"),
            "latest_checked_at": _iso_utc(row.get("latest_checked_at")),
            "uptime_24h": _uptime(checks_24h, up_24h),
            "uptime_7d": _uptime(checks_7d, up_7d),
            "checks_24h": checks_24h,
        })
    return jsonify(sites), 200


def _api_site_checks(site_id: int) -> tuple[Any, int]:
    """Return the recent checks of one site (spec section 11)."""
    limit = _parse_limit()
    if limit is None:
        return _api_error("The limit must be a positive number.", 400)

    try:
        with _api_connection() as conn:
            sites = db.list_sites(conn=conn)
            if not any(int(site.get("id") or 0) == site_id for site in sites):
                return _api_error("Unknown site.", 404)
            rows = db.get_recent_checks(site_id, limit=limit, conn=conn)
    except Exception as exc:  # noqa: BLE001 - any database failure is a 503
        logger.error("Reading the checks of site %s failed (%s)", site_id, type(exc).__name__)
        return _api_error("The check history is not available.", 503)

    checks = [
        {
            "id": row.get("id"),
            "site_id": row.get("site_id"),
            "checked_at": _iso_utc(row.get("checked_at")),
            "status_code": row.get("status_code"),
            "result": row.get("result"),
            "failed_stage": row.get("failed_stage"),
            "dns_ms": row.get("dns_ms"),
            "tcp_ms": row.get("tcp_ms"),
            "tls_ms": row.get("tls_ms"),
            "ttfb_ms": row.get("ttfb_ms"),
            "total_ms": row.get("total_ms"),
            "resolved_ip": row.get("resolved_ip"),
            "cert_expires_at": _iso_utc(row.get("cert_expires_at")),
            "cert_expiring_soon": bool(row.get("cert_expiring_soon")),
            "error_detail": row.get("error_detail"),
        }
        for row in rows
    ]
    return jsonify(checks), 200


def _api_incidents() -> tuple[Any, int]:
    """Return the incidents, newest first (spec section 11)."""
    limit = _parse_limit()
    if limit is None:
        return _api_error("The limit must be a positive number.", 400)

    try:
        with _api_connection() as conn:
            rows = db.list_incidents(limit=limit, conn=conn)
    except Exception as exc:  # noqa: BLE001 - any database failure is a 503
        logger.error("Reading the incidents failed (%s)", type(exc).__name__)
        return _api_error("The incidents are not available.", 503)

    incidents = [
        {
            "id": row.get("id"),
            "site_id": row.get("site_id"),
            "started_at": _iso_utc(row.get("started_at")),
            "ended_at": _iso_utc(row.get("ended_at")),
            "failure_type": row.get("failure_type"),
            "consecutive_failures": row.get("consecutive_failures"),
        }
        for row in rows
    ]
    return jsonify(incidents), 200


# --------------------------------------------------------------------------
# Dashboard (spec section 8.5)
# --------------------------------------------------------------------------

# The dashboard and the API read the database the same way: one connection
# per request, closed in a finally block, also when a query fails.
_request_connection = _api_connection

# CSS class of the status badge for a result code (spec section 8.3). A
# success is green, a slow but working site is amber, every other result is
# red and a site without any check is grey.
BADGE_CLASSES = {
    "OK": "ok",
    "SLOW": "slow",
    "DNS_SLOW": "slow",
}
NO_CHECKS_BADGE = "none"
NO_CHECKS_LABEL = "no checks yet"

# How many checks the site detail page reads from the database, how many of
# them the table shows and how many of them the stage chart shows.
SITE_DETAIL_CHECKS = 100
SITE_DETAIL_TABLE_ROWS = 20
CHART_STAGE_CHECKS = 30

# How many incidents the incidents page reads from the database.
INCIDENTS_LIMIT = 100


def _badge_class(result: Any) -> str:
    """Return the CSS class of the status badge for a result code."""
    if not result:
        return NO_CHECKS_BADGE
    return BADGE_CLASSES.get(result, "down")


def _uptime_text(value: float | None) -> str:
    """Return the uptime for the page.

    A window without a check has no uptime at all: it shows "no data" and
    never 0% - a site that was never checked is not the same as a site that
    was down.
    """
    if value is None:
        return "no data"
    return f"{value:g}%"


def _utc_text(value: Any) -> str:
    """Return the time of the latest check as UTC.

    A datetime without a time zone is read as UTC, a datetime in another
    time zone is converted to UTC. A value that is not a datetime is passed
    through unchanged.
    """
    if value is None:
        return "never"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(value)


def _dashboard_sites(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn the rows of ``site_summaries`` into the values the page shows."""
    sites = []
    for row in rows:
        result = row.get("latest_result")
        uptime_24h = _uptime(int(row.get("checks_24h") or 0), int(row.get("up_24h") or 0))
        uptime_7d = _uptime(int(row.get("checks_7d") or 0), int(row.get("up_7d") or 0))
        sites.append({
            "id": row.get("id"),
            "name": row.get("name"),
            "url": row.get("url"),
            "result": result or "",
            "badge_class": _badge_class(result),
            "badge_label": result or NO_CHECKS_LABEL,
            "uptime_24h": uptime_24h,
            "uptime_7d": uptime_7d,
            "uptime_24h_text": _uptime_text(uptime_24h),
            "uptime_7d_text": _uptime_text(uptime_7d),
            "last_checked_text": _utc_text(row.get("latest_checked_at")),
        })
    return sites


def _dashboard_counts(sites: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Return how many sites are up now, how many are down and how many
    have no check yet.

    A site is up when its latest result is one of the up results (a slow
    but working site is still up, spec section 8.3). A site without any
    check is neither up nor down - it is counted apart, so a site that was
    never checked is never reported as a problem.
    """
    up = sum(1 for site in sites if site["result"] in db.UP_RESULTS)
    down = sum(1 for site in sites if site["result"] and site["result"] not in db.UP_RESULTS)
    return up, down, len(sites) - up - down


def _dashboard_overview() -> tuple[Any, int]:
    """Render the overview page (spec section 8.5).

    A database failure is answered with a 503 page that holds a fixed
    message: no traceback, no exception text and nothing from the
    connection string (spec section 12).
    """
    try:
        with _request_connection() as conn:
            rows = db.site_summaries(conn=conn)
    except Exception as exc:  # noqa: BLE001 - any database failure is a 503
        # Only the type of the error is logged: the message of a database
        # error may contain the connection string (spec section 12).
        logger.error("Reading the site summaries failed (%s)", type(exc).__name__)
        return render_template("error.html"), 503

    sites = _dashboard_sites(rows)
    up_count, down_count, unknown_count = _dashboard_counts(sites)
    return render_template(
        "overview.html",
        sites=sites,
        total_count=len(sites),
        up_count=up_count,
        down_count=down_count,
        unknown_count=unknown_count,
    ), 200


def _row_id(row: dict[str, Any]) -> int | None:
    """Return the id of a row, or None when it has no usable id."""
    try:
        return int(row.get("id"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int_value(value: Any) -> int | None:
    """Return a value as an integer, or None when it is not one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> int | None:
    """Return a timing as an integer, or None when there is no timing.

    A stage that was never measured has no value at all: it stays None so
    that it becomes a gap in a chart and is never drawn as a zero.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def _chart_label(value: Any) -> str:
    """Return the label of a check on the time axis of a chart."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    return "" if value is None else str(value)


def _date_text(value: Any) -> str:
    """Return a date as YYYY-MM-DD, or an empty string when there is none."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).strftime("%Y-%m-%d")
    return "" if value is None else str(value)


def _site_detail_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn the check rows into the values the table of the page shows.

    Every value is a plain string or a number, so the template can print
    it with the normal escaping and can never inject markup (spec
    section 12).
    """
    rows = []
    for check in checks:
        result = check.get("result") or ""
        status_code = check.get("status_code")
        total_ms = check.get("total_ms")
        rows.append({
            "checked_at_text": _utc_text(check.get("checked_at")),
            "result": result,
            "badge_class": _badge_class(result),
            "badge_label": result or NO_CHECKS_LABEL,
            "failed_stage": check.get("failed_stage") or "",
            "status_code_text": "-" if status_code is None else str(status_code),
            "total_ms_text": "-" if total_ms is None else str(total_ms),
            "error_detail": check.get("error_detail") or "",
        })
    return rows


def _chart_data(site: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the data of the two charts of the site detail page.

    The checks are ordered oldest first, so a chart reads from left to
    right in time. A timing that was never measured stays None: it leaves
    a gap in a chart and is never drawn as a zero.
    """
    ordered = list(reversed(checks))
    labels = [_chart_label(check.get("checked_at")) for check in ordered]
    stages = {
        key: [_number(check.get(key)) for check in ordered]
        for key in ("dns_ms", "tcp_ms", "tls_ms", "ttfb_ms")
    }
    points = [
        {
            "checked_at": _chart_label(check.get("checked_at")),
            "result": check.get("result") or "",
            "error_detail": check.get("error_detail") or "",
            "total_ms": _number(check.get("total_ms")),
        }
        for check in ordered
    ]
    return {
        "site": {"name": site.get("name") or "", "url": site.get("url") or ""},
        "labels": labels,
        "total_ms": [_number(check.get("total_ms")) for check in ordered],
        "stages": stages,
        "checks": points,
    }


def _site_detail(site_id: int) -> tuple[Any, int]:
    """Render the detail page of one site (spec section 8.5).

    The page reads the site from the summaries and its checks from the
    check history, with one connection for the whole request. A database
    failure is answered with the same 503 page as the overview: no
    traceback, no exception text and nothing from the connection string
    (spec section 12). An unknown site is answered with a 404 page.
    """
    try:
        with _request_connection() as conn:
            rows = db.site_summaries(conn=conn)
            site = next((row for row in rows if _row_id(row) == site_id), None)
            if site is None:
                return render_template("not_found.html"), 404
            checks = db.get_recent_checks(
                site_id, limit=SITE_DETAIL_CHECKS, conn=conn
            )
    except Exception as exc:  # noqa: BLE001 - any database failure is a 503
        # Only the type of the error is logged: the message of a database
        # error may contain the connection string (spec section 12).
        logger.error("Reading site %s failed (%s)", site_id, type(exc).__name__)
        return render_template("error.html"), 503

    latest = checks[0] if checks else {}
    result = site.get("latest_result")
    uptime_24h = _uptime(
        int(site.get("checks_24h") or 0), int(site.get("up_24h") or 0)
    )
    uptime_7d = _uptime(
        int(site.get("checks_7d") or 0), int(site.get("up_7d") or 0)
    )

    view = {
        "id": site.get("id"),
        "name": site.get("name") or "",
        "url": site.get("url") or "",
        "result": result or "",
        "badge_class": _badge_class(result),
        "badge_label": result or NO_CHECKS_LABEL,
        "uptime_24h_text": _uptime_text(uptime_24h),
        "uptime_7d_text": _uptime_text(uptime_7d),
        "last_checked_text": _utc_text(site.get("latest_checked_at")),
        "cert_expires_text": _date_text(latest.get("cert_expires_at")),
        "cert_expiring_soon": bool(latest.get("cert_expiring_soon")),
        "resolved_ip": latest.get("resolved_ip") or "",
    }

    return render_template(
        "site_detail.html",
        site=view,
        checks=_site_detail_checks(checks[:SITE_DETAIL_TABLE_ROWS]),
        has_checks=bool(checks),
        chart_data=_chart_data(view, checks),
    ), 200


def _utc_now() -> datetime:
    """Return the current time in UTC.

    It is a function of its own so that a test can hold the clock still
    while it looks at an incident that is still going on.
    """
    return datetime.now(UTC)


def _duration_text(seconds: float) -> str:
    """Return a duration as a short text, e.g. "1h 30m 00s".

    Only the two biggest units are shown for a long duration, so the text
    stays short enough for a table cell on a phone.
    """
    total = max(int(seconds), 0)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _elapsed_text(start: Any, end: Any) -> str:
    """Return how long something lasted, or "-" when that is not known.

    A time without a time zone is read as UTC, so a duration is never
    computed between two times that mean different things.
    """
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return "-"
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return _duration_text((end - start).total_seconds())


def _incident_view(
    row: dict[str, Any], names: dict[int | None, Any], now: datetime
) -> dict[str, Any]:
    """Turn one incident row into the values the table of the page shows.

    The name of the site comes from the site summaries. A site that is
    gone keeps its number in the page, so the incident is still readable
    (spec section 8.5). Every value is a plain string, so the template
    can print it with the normal escaping.
    """
    site_id = _int_value(row.get("site_id"))
    name = names.get(site_id)
    started = row.get("started_at")
    ended = row.get("ended_at")
    # An incident without an end is still going on: its duration is the
    # time since it started.
    ongoing = ended is None
    failures = row.get("consecutive_failures")

    if name:
        site_name = str(name)
    elif site_id is not None:
        site_name = f"site #{site_id}"
    else:
        site_name = "unknown site"

    return {
        "id": row.get("id"),
        "site_id": site_id,
        "site_name": site_name,
        "site_known": bool(name),
        "started_text": _utc_text(started),
        "ended_text": _utc_text(ended),
        "ongoing": ongoing,
        "duration_text": _elapsed_text(started, now if ongoing else ended),
        "failure_type": row.get("failure_type") or "-",
        "failures_text": "-" if failures is None else str(failures),
    }


def _incident_sort_key(row: dict[str, Any]) -> tuple[datetime, int]:
    """Return the key that puts the newest incident first.

    The database already sorts the incidents by their start, but the page
    sorts them again, so the order of the page never depends on the order
    of the rows it gets.
    """
    started = row.get("started_at")
    if isinstance(started, datetime):
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
    else:
        started = datetime.min.replace(tzinfo=UTC)
    return (started, _int_value(row.get("id")) or 0)


def _incidents_page() -> tuple[Any, int]:
    """Render the incidents page (spec section 8.5).

    The page reads the sites (for their names) and the incidents with one
    connection for the whole request. A database failure is answered with
    the same 503 page as the overview: no traceback, no exception text and
    nothing from the connection string (spec section 12).
    """
    try:
        with _request_connection() as conn:
            sites = db.site_summaries(conn=conn)
            rows = db.list_incidents(limit=INCIDENTS_LIMIT, conn=conn)
    except Exception as exc:  # noqa: BLE001 - any database failure is a 503
        # Only the type of the error is logged: the message of a database
        # error may contain the connection string (spec section 12).
        logger.error("Reading the incidents failed (%s)", type(exc).__name__)
        return render_template("error.html"), 503

    names = {_int_value(row.get("id")): row.get("name") for row in sites}
    now = _utc_now()
    ordered = sorted(rows, key=_incident_sort_key, reverse=True)
    incidents = [_incident_view(row, names, now) for row in ordered]
    return render_template("incidents.html", incidents=incidents), 200


# --------------------------------------------------------------------------
# The values of a site (spec section 8.1)
# --------------------------------------------------------------------------

def _as_int(value: Any) -> int | None:
    """Return a value as an integer, or None when it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdecimal():
            return int(text)
    return None


def _as_bool(value: Any) -> bool | None:
    """Return a value as a boolean, or None when it is not one."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "on", "yes"):
            return True
        if text in ("false", "0", "off", "no", ""):
            return False
    return None


def _site_values(
    data: dict[str, Any], current: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    """Return the values of a site, or a message that says what is wrong.

    ``current`` holds the values of the site that is changed: a field that
    is not sent keeps its value. Without it a new site is created, and
    then the name and the URL are required.
    """
    creating = current is None
    values: dict[str, Any] = {}
    if current:
        values = {key: current[key] for key in SITE_FIELDS if key in current}

    # The name
    if "name" in data or creating:
        name = data.get("name")
        if not isinstance(name, str):
            return None, "The name must be text."
        name = name.strip()
        if not name:
            return None, "The name is required."
        if len(name) > MAX_NAME_LENGTH:
            return None, "The name is too long."
        values["name"] = name

    # The URL
    if "url" in data or creating:
        url = data.get("url")
        if not isinstance(url, str):
            return None, "The URL must be text."
        url = url.strip()
        if not url:
            return None, "The URL is required."
        if len(url) > MAX_URL_LENGTH:
            return None, "The URL is too long."
        valid, _scheme, _host, _port, _path, error, _hint = parse_url(url)
        if not valid:
            return None, f"The URL is not usable: {error}"
        values["url"] = url

    # The expected status code
    if "expected_status" in data:
        raw = data.get("expected_status")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            values["expected_status"] = DEFAULT_EXPECTED_STATUS
        else:
            status = _as_int(raw)
            if status is None:
                return None, "The expected status must be a number."
            if not 100 <= status <= 599:
                return None, "The expected status must be a number between 100 and 599."
            values["expected_status"] = status
    elif creating:
        values["expected_status"] = DEFAULT_EXPECTED_STATUS

    # The keyword
    if "keyword" in data:
        keyword = data.get("keyword")
        if keyword is None:
            values["keyword"] = None
        elif isinstance(keyword, str):
            keyword = keyword.strip()
            if len(keyword) > MAX_KEYWORD_LENGTH:
                return None, "The keyword is too long."
            values["keyword"] = keyword or None
        else:
            return None, "The keyword must be text."
    elif creating:
        values["keyword"] = None

    # The slow threshold
    if "slow_threshold_ms" in data:
        raw = data.get("slow_threshold_ms")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            values["slow_threshold_ms"] = DEFAULT_SLOW_THRESHOLD_MS
        else:
            threshold = _as_int(raw)
            if threshold is None:
                return None, "The slow threshold must be a number of milliseconds."
            if threshold <= 0:
                return None, "The slow threshold must be a positive number of milliseconds."
            values["slow_threshold_ms"] = threshold
    elif creating:
        values["slow_threshold_ms"] = DEFAULT_SLOW_THRESHOLD_MS

    # The enabled flag
    if "enabled" in data:
        enabled = _as_bool(data.get("enabled"))
        if enabled is None:
            return None, "The enabled flag must be true or false."
        values["enabled"] = enabled
    elif creating:
        # An admin checkbox is not ticked when the box is unchecked, so the
        # new site defaults to being monitored. When editing, an absent
        # field keeps the current value: unchecking the box really disables
        # the site instead of silently leaving it on.
        values["enabled"] = current.get("enabled", True) if current else True

    return values, None


def _form_values(form: Any) -> dict[str, Any]:
    """Return the values of a form of the admin area.

    A checkbox that is not ticked is not sent at all, so the flag is read
    from the presence of the field: an unchecked box keeps its default
    value.
    """
    return dict(form)


def _max_sites() -> int:
    """Return the number of sites the application monitors at most."""
    return int(config.rate_limit.max_sites)


def _safe_next(value: Any) -> str:
    """Return the page to go to after the login.

    Only a path of this application is allowed, so the login can never be
    used to send a visitor to another site.
    """
    if isinstance(value, str) and value.startswith("/") and not value.startswith("//"):
        return value
    return url_for("admin_sites")


def call_runner() -> dict[str, object]:
    """Run the check engine and return its summary.

    The runner is called through the ``app.runner`` module, so a runner
    that was replaced there is used. A name that was replaced on this module
    wins over it, which keeps both places patchable in a test.
    """
    from . import runner

    func = globals().get("run_all_checks")
    if func is _ORIGINAL_RUN_ALL_CHECKS:
        func = runner.run_all_checks
    return func()


def register(app: Flask) -> None:
    """Register all routes with the Flask application."""

    # werkzeug logs the whole request line, query string included, so the
    # scheduler token would end up in the log. Only warnings and errors are
    # kept from it (spec section 12).
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @app.route("/health")
    def health():
        """Lightweight health check endpoint - no DB or network work."""
        return jsonify({
            "status": "healthy",
            "service": "whydown",
            "version": "0.1.0"
        }), 200

    @app.route("/run-checks", methods=["GET", "POST"])
    @require_cron_token
    def run_checks():
        """Check every enabled site and return the summary of the run.

        The route is called by the external scheduler (spec section 11). The
        checks run synchronously, so the summary of the run is in the
        response when it arrives.
        """
        try:
            summary = call_runner()
            response = jsonify(summary)
        except Exception as exc:  # noqa: BLE001 - any failure must become a 500
            # Only the type of the error is logged: the message of an
            # exception may contain anything and must never leak a secret.
            logger.error("Check run failed (%s)", type(exc).__name__)
            return jsonify({"error": "Check run failed"}), 500

        return response, 200

    # Import and register other route groups here
    # For now, we'll just register the health endpoint
    # Other routes will be implemented in later phases

    register_dashboard_routes(app)
    register_api_routes(app)
    register_api_write_routes(app)
    register_admin_routes(app)


# Placeholder functions to be implemented in later phases
def register_dashboard_routes(app: Flask) -> None:
    """Register the dashboard (spec section 8.5).

    The dashboard is public and read-only: it reads the state of the sites
    and never changes anything.
    """

    @app.get("/")
    def dashboard():
        """The overview page: the state of every monitored site."""
        return _dashboard_overview()

    @app.get("/sites/<int:site_id>")
    def site_detail(site_id: int):
        """The detail page of one site: its timings and its history."""
        return _site_detail(site_id)

    @app.get("/incidents")
    def incidents():
        """The list of the incidents, newest first."""
        return _incidents_page()


def register_api_routes(app: Flask) -> None:
    """Register the read-only JSON API (spec section 11).

    The API is public and read-only: it never changes a site and never
    starts a check. Every response says that it must not be cached, so a
    client never shows an old state of a site.
    """

    @app.get("/api/sites")
    def api_sites():
        """List the sites with their current state."""
        return _api_sites()

    @app.get("/api/sites/<int:site_id>/checks")
    def api_site_checks(site_id: int):
        """Return the recent checks of one site."""
        return _api_site_checks(site_id)

    @app.get("/api/incidents")
    def api_incidents():
        """Return the incidents, newest first."""
        return _api_incidents()

    @app.after_request
    def _no_store(response):
        """Never let a client cache an API answer."""
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(405)
    def _method_not_allowed(error):
        """Answer a wrong method with a JSON error, not with an HTML page."""
        if request.path.startswith("/api/"):
            return jsonify({"error": "Method not allowed."}), 405
        return error


def register_api_write_routes(app: Flask) -> None:
    """Register the write routes of the JSON API (spec section 11).

    Every route needs a logged in admin and a CSRF token that belongs to
    the session. A request without a session is answered with a 401, a
    request without a token with a 403.
    """

    @app.post("/api/sites")
    @auth.admin_required
    @auth.csrf_protect
    def api_add_site():
        """Add a site and answer with its new id."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _api_error("The body must be a JSON object.", 400)

        values, error = _site_values(data)
        if error:
            return _api_error(error, 400)

        try:
            with _api_connection() as conn:
                if len(db.list_sites(conn=conn)) >= _max_sites():
                    return _api_error("Too many sites are monitored.", 400)
                site_id = db.add_site(conn=conn, **values)
        except Exception as exc:  # noqa: BLE001 - any database failure is a 503
            logger.error("Adding a site failed (%s)", type(exc).__name__)
            return _api_error("The site could not be stored.", 503)

        return jsonify({"id": site_id, **values}), 201

    @app.put("/api/sites/<int:site_id>")
    @auth.admin_required
    @auth.csrf_protect
    def api_update_site(site_id: int):
        """Change the values of a site."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _api_error("The body must be a JSON object.", 400)

        try:
            with _api_connection() as conn:
                current = db.get_site(site_id, conn=conn)
        except Exception as exc:  # noqa: BLE001 - any database failure is a 503
            logger.error("Reading site %s failed (%s)", site_id, type(exc).__name__)
            return _api_error("The site is not available.", 503)

        if current is None:
            return _api_error("Unknown site.", 404)

        values, error = _site_values(data, current=current)
        if error:
            return _api_error(error, 400)

        try:
            with _api_connection() as conn:
                updated = db.update_site(site_id, conn=conn, **values)
        except Exception as exc:  # noqa: BLE001 - any database failure is a 503
            logger.error("Updating site %s failed (%s)", site_id, type(exc).__name__)
            return _api_error("The site could not be changed.", 503)

        if not updated:
            return _api_error("Unknown site.", 404)

        return jsonify({"id": site_id, **values}), 200

    @app.delete("/api/sites/<int:site_id>")
    @auth.admin_required
    @auth.csrf_protect
    def api_delete_site(site_id: int):
        """Remove a site and everything that belongs to it."""
        try:
            with _api_connection() as conn:
                removed = db.delete_site(site_id, conn=conn)
        except Exception as exc:  # noqa: BLE001 - any database failure is a 503
            logger.error("Deleting site %s failed (%s)", site_id, type(exc).__name__)
            return _api_error("The site could not be removed.", 503)

        if not removed:
            return _api_error("Unknown site.", 404)

        return "", 204


def register_admin_routes(app: Flask) -> None:
    """Register the admin area (spec section 8.5 and 12).

    The admin area is the only part of the application that changes
    something. Every write route needs a logged in admin and a CSRF token
    that belongs to the session (spec section 12).
    """

    @app.get("/admin/login")
    def admin_login():
        """The login page of the admin area."""
        if auth.is_admin():
            return redirect(url_for("admin_sites"))
        return render_template(
            "admin_login.html", admin_available=auth.admin_configured()
        ), 200

    @app.post("/admin/login")
    @auth.csrf_protect
    def admin_login_submit():
        """Check the password and open the session of the admin.

        The password is compared with hmac.compare_digest. A failed login
        is written to the log without the password, and a burst of failed
        logins locks the address out for a while.
        """
        address = auth.client_address()

        if auth.login_locked(address):
            logger.warning(
                "Admin login refused for %s: too many failed attempts", address
            )
            return render_template(
                "admin_login.html",
                admin_available=True,
                error="Too many failed attempts. Please try again in a few minutes.",
            ), 429

        if not auth.admin_configured():
            logger.warning("Admin login refused: no admin password is configured")
            return render_template("admin_login.html", admin_available=False), 503

        given = request.form.get(auth.PASSWORD_FIELD, "")
        if not auth.password_matches(given):
            auth.record_login_failure(address)
            # The log holds the address and nothing else: the password must
            # never end up in a log (spec section 12).
            logger.warning("Admin login failed for %s", address)
            return render_template(
                "admin_login.html",
                admin_available=True,
                error="The password is not correct.",
            ), 401

        auth.clear_login_failures(address)
        auth.start_admin_session()
        return redirect(_safe_next(request.form.get("next")))

    @app.post("/admin/logout")
    @auth.admin_required
    @auth.csrf_protect
    def admin_logout():
        """Close the session of the admin."""
        auth.end_admin_session()
        return redirect(url_for("admin_login"))

    @app.get("/admin/sites")
    @auth.admin_required
    def admin_sites():
        """The page that manages the monitored sites."""
        try:
            with _request_connection() as conn:
                sites = db.list_sites(conn=conn)
        except Exception as exc:  # noqa: BLE001 - any database failure is a 503
            logger.error("Reading the sites failed (%s)", type(exc).__name__)
            return render_template("error.html"), 503

        return render_template("admin_sites.html", sites=sites), 200

    @app.post("/admin/sites")
    @auth.admin_required
    @auth.csrf_protect
    def admin_add_site():
        """Add a site from the admin page."""
        values, error = _site_values(_form_values(request.form))
        if error:
            return _admin_page_error(error), 400

        try:
            with _request_connection() as conn:
                if len(db.list_sites(conn=conn)) >= _max_sites():
                    return _admin_page_error("Too many sites are monitored."), 400
                db.add_site(conn=conn, **values)
        except Exception as exc:  # noqa: BLE001 - any database failure is a 503
            logger.error("Adding a site failed (%s)", type(exc).__name__)
            return render_template("error.html"), 503

        return redirect(url_for("admin_sites"))

    @app.post("/admin/sites/<int:site_id>/edit")
    @auth.admin_required
    @auth.csrf_protect
    def admin_update_site(site_id: int):
        """Change a site from the admin page."""
        try:
            with _request_connection() as conn:
                current = db.get_site(site_id, conn=conn)
        except Exception as exc:  # noqa: BLE001 - any database failure is a 503
            logger.error("Reading site %s failed (%s)", site_id, type(exc).__name__)
            return render_template("error.html"), 503

        if current is None:
            return render_template("not_found.html"), 404

        values, error = _site_values(_form_values(request.form), current=current)
        if error:
            return _admin_page_error(error), 400

        try:
            with _request_connection() as conn:
                db.update_site(site_id, conn=conn, **values)
        except Exception as exc:  # noqa: BLE001 - any database failure is a 503
            logger.error("Updating site %s failed (%s)", site_id, type(exc).__name__)
            return render_template("error.html"), 503

        return redirect(url_for("admin_sites"))

    @app.post("/admin/sites/<int:site_id>/delete")
    @auth.admin_required
    @auth.csrf_protect
    def admin_delete_site(site_id: int):
        """Remove a site from the admin page."""
        try:
            with _request_connection() as conn:
                removed = db.delete_site(site_id, conn=conn)
        except Exception as exc:  # noqa: BLE001 - any database failure is a 503
            logger.error("Deleting site %s failed (%s)", site_id, type(exc).__name__)
            return render_template("error.html"), 503

        if not removed:
            return render_template("not_found.html"), 404

        return redirect(url_for("admin_sites"))


def _admin_page_error(message: str) -> Any:
    """Render the admin page with a message that says what is wrong."""
    try:
        with _request_connection() as conn:
            sites = db.list_sites(conn=conn)
    except Exception:  # noqa: BLE001 - the message is more important here
        sites = []
    return render_template("admin_sites.html", sites=sites, error=message)


def register_checker_routes(app: Flask) -> None:
    """Register checker routes (to be implemented in Phase 2)."""
