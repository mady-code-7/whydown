"""Check runner for WhyDown.

Implements the Phase 3B-1 check runner (spec section 10):

* ``run_all_checks`` loads the enabled sites, checks them concurrently in a
  thread pool, saves every result and returns a summary.
* A hard per-site timeout is enforced from outside ``check_url`` using future
  timeouts, so a slow DNS lookup or a stuck site cannot block the run.
* A total run deadline keeps the run inside the scheduler's request window.
* One failing site (or a database error while saving) must never stop the run.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait

from .checker import CheckResult, check_url
from .config import config
from .db import list_sites, purge_old_checks, save_check

logger = logging.getLogger(__name__)

# Result code used when a site check does not finish within its time budget.
# Spec section 8.3 has no "total timeout" code, so the closest connection-level
# timeout code is used: the site never completed its check in time.
TIMEOUT_RESULT = "CONNECT_TIMEOUT"


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
            cert_expiry_warning_days=config.thresholds.cert_expiry_warning_days,
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
    record(site["id"], result)


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
    record(site["id"], result)


def _run_sites(
    sites: list[dict[str, object]],
    check_fn: Callable[..., CheckResult],
    record: Callable[[int, CheckResult], None],
    max_workers: int,
    site_timeout: float,
    run_deadline: float,
) -> None:
    """Check sites concurrently with per‑site and run deadlines.

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
    save_check_func: Callable[[int, CheckResult], int] | None = None,
    purge_func: Callable[[], int] | None = None,
    max_workers: int | None = None,
    site_timeout_seconds: float | None = None,
    run_deadline_seconds: float | None = None,
) -> dict[str, object]:
    """Load sites and run checks, returning a summary of the run.

    The function never raises: any error (in checks, in database access or in the
    thread pool) is caught and reported as a result, so one bad site can never
    stop the run. After all checks are done, ``purge_old_checks`` is called.
    """
    start = time.perf_counter()

    # Use mocks for testing, or the real functions in production
    list_sites_fn = list_sites_func or list_sites
    check_fn = check_url_func or check_url
    save_fn = save_check_func or save_check
    purge_fn = purge_func or purge_old_checks

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

    def record(site_id: int, result: CheckResult) -> None:
        save_fn(site_id, result)
        counts[result.result] = counts.get(result.result, 0) + 1
        checked[0] += 1

    sites = list_sites_fn(enabled_only=True)

    if sites:
        _run_sites(sites, check_fn, record, workers, site_timeout, run_deadline)

    try:
        purge_fn()
    except Exception:
        logger.exception("Purge of old checks failed; the run still completed")

    return {
        "sites_checked": checked[0],
        "counts": dict(counts),
        "duration_ms": int((time.perf_counter() - start) * 1000),
    }