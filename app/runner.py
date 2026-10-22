"""Check runner for WhyDown.

Implements the Phase 3B-1 check runner (spec section 10):

* ``run_all_checks`` opens **one** database connection for the whole run and
  hands it to every database call. Opening a connection is by far the most
  expensive database operation (about 2.5 s on the free tier), so one
  connection per call would dominate the runtime of a run.
* The connection belongs to the main thread. The workers of the thread pool
  run ``check_url`` and nothing else, so a connection is never used from two
  threads at the same time.
* Every result is saved from the main thread as soon as its future
  completes, and ``save_check`` commits the row. A crash or a run deadline
  therefore keeps the results that were collected so far.
* A hard per-site timeout is enforced from outside ``check_url`` using future
  timeouts, so a slow DNS lookup or a stuck site cannot block the run.
* A total run deadline keeps the run inside the scheduler's request window.
* When the incident state machine opens or closes an incident, the matching
  alert is sent (spec section 8.4). A failing alert never stops the run.
* A certificate that expires soon gets its warning (spec section 8.3.1). The
  warning is sent once per tier; a failing warning never stops the run.
* One failing site (or a failed save) must never stop the run.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any

from .alerts import KIND_DOWN, KIND_RECOVERY, send_alert, send_cert_alert
from .checker import CheckResult, check_url
from .config import config
from .db import _connect, get_latest_incident, list_sites, purge_old_checks, save_check
from .incidents import process_check

logger = logging.getLogger(__name__)

# Result code used when a site check does not finish within its time budget.
# Spec section 8.3 has no "total timeout" code, so the closest connection-level
# timeout code is used: the site never completed its check in time.
TIMEOUT_RESULT = "CONNECT_TIMEOUT"


def _close_connection(conn: Any) -> None:
    """Close a connection and ignore any error while doing so."""
    if conn is None:
        return
    try:
        conn.close()
    except Exception as exc:  # noqa: BLE001 - a failed close must not stop the run
        logger.warning("Closing the database connection failed (%s)", type(exc).__name__)


class _RunConnection:
    """The single database connection of one check run.

    The connection is opened once, used by the main thread only and closed
    when the run ends. A failed save triggers one attempt to reopen it, so a
    broken connection (an aborted transaction) does not take the rest of the
    run with it.

    When the reopen fails the connection is given up on: a database that is
    really down would otherwise cost a reconnect attempt (about 2.5 s) for
    every site of the run.
    """

    def __init__(self, connect_func: Callable[[], Any]) -> None:
        self._connect_func = connect_func
        # A failure here is deliberately not caught: without a connection the
        # run cannot store a single result, so the error travels to
        # /run-checks, which answers with a generic 500.
        self.conn: Any = connect_func()
        self.save_errors = 0
        self.broken = False

    def reopen(self) -> None:
        """Try once to replace the connection with a fresh one.

        When the new connection cannot be opened, the connection is marked as
        broken: the run continues, but no further connect is attempted.
        """
        if self.broken:
            return
        try:
            new_conn = self._connect_func()
        except Exception as exc:  # noqa: BLE001 - a failed reopen must not stop the run
            # Only the type of the error is logged: the message of a database
            # error may contain the connection string (spec section 12).
            logger.error(
                "The database connection could not be reopened (%s)", type(exc).__name__
            )
            self.broken = True
            return
        old_conn, self.conn = self.conn, new_conn
        _close_connection(old_conn)

    def close(self) -> None:
        """Close the connection of the run."""
        _close_connection(self.conn)


def _check_site(site: dict[str, object], check_fn: Callable[..., CheckResult]) -> CheckResult:
    """Run one site's check, never raising.

    This wrapper is a safety net. The ``check_url`` function itself never raises
    (section 10), but if a test replaces it with a mock that can raise, this
    catches it and records an ``INTERNAL_ERROR`` so that a single bad site can
    never stop a run.
    """
    try:
        return check_fn(
            site["url"],
            slow_threshold_ms=int(site["slow_threshold_ms"]),
            # The warning window is at least as wide as the widest warning
            # tier (14 days by default), so the ``cert_expiring_soon`` flag
            # is set in time for every tier (spec section 8.3.1).
            cert_expiry_warning_days=config.thresholds.cert_warning_days,
            expected_status=int(site["expected_status"]),
            expected_keyword=site["keyword"],
        )
    except Exception as exc:
        logger.exception("Unexpected error while checking %s", site.get("url"))
        result = CheckResult()
        result.result = "INTERNAL_ERROR"
        result.failed_stage = "unknown"
        result.error_message = f"Unexpected error: {exc}"
        result.total_ms = 0.0
        return result


def _collect_result(future: Future[CheckResult], site: dict[str, object], record) -> None:
    """Obtain the result of a check future and save it.

    If the future raises (only possible with a mock in tests), it is recorded as
    an ``INTERNAL_ERROR`` to match the safety net in ``_check_site``.

    This runs in the main thread, so the database connection is only ever
    used by one thread at a time.
    """
    try:
        result = future.result()
    except Exception as exc:
        logger.exception("Check raised for %s", site.get("url"))
        result = CheckResult()
        result.result = "INTERNAL_ERROR"
        result.failed_stage = "unknown"
        result.error_message = f"Check raised: {exc}"
        result.total_ms = 0.0
    record(site, result)


def _record_timeout(site: dict[str, object], budget_seconds: float, reason: str, record) -> None:
    """Record a site that did not finish within its budget.

    The timeout is stored as ``CONNECT_TIMEOUT`` (spec section 8.3) with a
    failure stage of ``tcp`` because the check never reached completion.
    """
    result = CheckResult()
    result.result = TIMEOUT_RESULT
    result.failed_stage = "tcp"
    result.error_message = f"{reason}: check did not complete within {budget_seconds}s"
    result.total_ms = budget_seconds * 1000
    record(site, result)


def _send_certificate_alert(
    conn: Any,
    site: dict[str, object],
    result: CheckResult,
    send_cert_alert_func: Callable[..., bool],
) -> None:
    """Send the certificate-expiry warning of a check, if it is needed.

    The warning is only sent for a check whose certificate expires soon
    (spec section 8.3.1). A failure of the alert - a broken channel, a bad
    token, a database error - is logged and never stops the run for the
    other sites (spec section 10).
    """
    site_id = site.get("id")
    if not getattr(result, "cert_expiring_soon", False):
        return

    try:
        send_cert_alert_func(
            conn,
            site,
            result,
            config.thresholds.cert_alert_thresholds,
        )
    except Exception as exc:  # noqa: BLE001 - a failed alert must not stop the run
        logger.error(
            "The certificate warning of site %s could not be sent (%s)",
            site_id,
            type(exc).__name__,
        )


def _send_incident_alert(
    conn: Any,
    site: dict[str, object],
    kind: str,
    get_incident_func: Callable[..., dict[str, object] | None],
    send_alert_func: Callable[..., bool],
) -> None:
    """Send the DOWN or RECOVERY alert of an incident.

    The incident is read from the database: it holds the start, the end and
    the failure type that the message needs. A failure of the alert - a
    broken channel, a bad token, a database error - is logged and never
    stops the run for the other sites (spec section 10).
    """
    site_id = site.get("id")
    try:
        incident = get_incident_func(site_id, conn=conn)
        if incident is None:
            logger.error(
                "The %s alert of site %s was skipped: no incident was found",
                kind,
                site_id,
            )
            return
        send_alert_func(conn, site, incident, kind)
    except Exception as exc:  # noqa: BLE001 - a failed alert must not stop the run
        logger.error(
            "The %s alert of site %s could not be sent (%s)",
            kind,
            site_id,
            type(exc).__name__,
        )


def _run_sites(
    sites: list[dict[str, object]],
    check_fn: Callable[..., CheckResult],
    record: Callable[[dict[str, object], CheckResult], None],
    max_workers: int,
    site_timeout: float,
    run_deadline: float,
) -> None:
    """Check sites concurrently with per‑site and run deadlines.

    The worker threads only run the check itself; every result is collected
    and stored by the main thread, which is the only thread that touches the
    database connection.

    The function never lets a single site failure or timeout block the run, and
    it returns after the run deadline has passed or all sites have been checked.
    """
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        future_to_site: dict[Future[CheckResult], dict[str, object]] = {}
        for site in sites:
            future = executor.submit(_check_site, site, check_fn)
            future_to_site[future] = site
        submitted_at = {f: time.perf_counter() for f in future_to_site}
        deadline = time.perf_counter() + run_deadline
        pending = set(future_to_site)

        while pending:
            now = time.perf_counter()
            if now >= deadline:
                break

            # Per‑site timeout enforcement
            stuck = [f for f in pending if now - submitted_at[f] >= site_timeout]
            for future in stuck:
                pending.discard(future)
                _record_timeout(future_to_site[future], site_timeout, "site timeout", record)
            if not pending:
                break

            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            done, pending = wait(pending, timeout=min(site_timeout, remaining))
            for future in done:
                _collect_result(future, future_to_site[future], record)

        # Run deadline – any still pending futures have exceeded the total budget
        for future in pending:
            _record_timeout(future_to_site[future], run_deadline, "run deadline", record)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def run_all_checks(
    *,
    list_sites_func: Callable[..., list[dict[str, object]]] | None = None,
    check_url_func: Callable[..., CheckResult] | None = None,
    save_check_func: Callable[..., int] | None = None,
    purge_func: Callable[..., int] | None = None,
    process_check_func: Callable[..., str | None] | None = None,
    send_alert_func: Callable[..., bool] | None = None,
    send_cert_alert_func: Callable[..., bool] | None = None,
    get_incident_func: Callable[..., dict[str, object] | None] | None = None,
    connect_func: Callable[[], Any] | None = None,
    max_workers: int | None = None,
    site_timeout_seconds: float | None = None,
    run_deadline_seconds: float | None = None,
) -> dict[str, object]:
    """Load sites and run checks, returning a summary of the run.

    One database connection is opened at the start of the run and used for
    every database call; it is closed when the run ends, also when the run
    fails. The connection is used by the main thread only.

    A failed save is counted in the summary as ``save_errors`` and the
    connection is reopened once; the run continues with the remaining sites.

    Every stored check is handed to the incident state machine, which opens
    and closes the incidents. A failure of the state machine never stops the
    run. An incident that was opened or closed gets its alert - a DOWN
    alert when it opens, a RECOVERY alert when it closes. A failing alert
    never stops the run either.
    """
    start = time.perf_counter()

    # Use mocks for testing, or the real functions in production
    list_sites_fn = list_sites_func or list_sites
    check_fn = check_url_func or check_url
    save_fn = save_check_func or save_check
    purge_fn = purge_func or purge_old_checks
    process_check_fn = process_check_func or process_check
    send_alert_fn = send_alert_func or send_alert
    send_cert_alert_fn = send_cert_alert_func or send_cert_alert
    get_incident_fn = get_incident_func or get_latest_incident
    connect_fn = connect_func or _connect

    # Use overrides if passed, or fall back to config values
    workers = max_workers if max_workers is not None else config.runner.max_workers
    site_timeout = (
        site_timeout_seconds
        if site_timeout_seconds is not None
        else config.runner.site_timeout_seconds
    )
    run_deadline = (
        run_deadline_seconds
        if run_deadline_seconds is not None
        else config.runner.run_deadline_seconds
    )

    counts: dict[str, int] = {}
    checked = [0]  # mutable container to allow increments inside _record

    # The one connection of this run. A failure to open it is not caught:
    # /run-checks turns it into a generic 500.
    connection = _RunConnection(connect_fn)

    def record(site: dict[str, object], result: CheckResult) -> None:
        """Save one result, count it and feed the incident state machine.

        This runs in the main thread only, so the database connection is never
        used by two threads at the same time.
        """
        site_id = site["id"]
        counts[result.result] = counts.get(result.result, 0) + 1
        checked[0] += 1
        try:
            save_fn(site_id, result, conn=connection.conn)
        except Exception as exc:  # noqa: BLE001 - a failed save must not stop the run
            connection.save_errors += 1
            # Only the type of the error is logged: the message of a database
            # error may contain the connection string (spec section 12).
            logger.error(
                "Saving the check of site %s failed (%s)", site_id, type(exc).__name__
            )
            # One attempt to get a working connection back; the run continues
            # either way.
            connection.reopen()
            return

        # The check is stored, so the incident state machine can look at it.
        # A failure of one site must never stop the run for the other sites.
        outcome: str | None = None
        try:
            outcome = process_check_fn(connection.conn, site, result)
        except Exception as exc:  # noqa: BLE001 - a failed incident must not stop the run
            logger.error(
                "The incident state of site %s could not be updated (%s)",
                site_id,
                type(exc).__name__,
            )

        # An incident that was opened or closed gets its alert: a DOWN alert
        # when the incident opens, a RECOVERY alert when it closes (spec
        # section 8.4). A failing alert never stops the run.
        if outcome == "opened":
            _send_incident_alert(
                connection.conn, site, KIND_DOWN, get_incident_fn, send_alert_fn
            )
        elif outcome == "closed":
            _send_incident_alert(
                connection.conn, site, KIND_RECOVERY, get_incident_fn, send_alert_fn
            )

        # A certificate that expires soon gets a warning (spec section 8.3.1).
        # The warning is sent once per threshold; the alert log remembers what
        # was already sent. A failing warning never stops the run.
        _send_certificate_alert(
            connection.conn, site, result, send_cert_alert_fn
        )

    try:
        sites = list_sites_fn(conn=connection.conn, enabled_only=True)

        if sites:
            _run_sites(sites, check_fn, record, workers, site_timeout, run_deadline)

        try:
            purge_fn(conn=connection.conn)
        except Exception as exc:  # noqa: BLE001 - a failed purge must not stop the run
            logger.error(
                "Purge of old checks failed (%s); the run still completed",
                type(exc).__name__,
            )
    finally:
        connection.close()

    summary: dict[str, object] = {
        "sites_checked": checked[0],
        "counts": dict(counts),
        "duration_ms": int((time.perf_counter() - start) * 1000),
    }
    if connection.save_errors:
        summary["save_errors"] = connection.save_errors
    return summary
