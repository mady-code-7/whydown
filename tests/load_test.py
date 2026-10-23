"""Load test for WhyDown Phase 6 part A.

Starts the mock failure server, builds 20 fake sites with various speeds
and failure modes, runs the check runner, and measures whether the run
finishes within the time budget (spec T18).

Run via: python -m tests.load_test
"""

from __future__ import annotations

import sys
import time
from typing import Any

from app.checker import CheckResult, check_url
from app.config import config
from app.runner import run_all_checks
from tests.mock_server import MockServer

#: Total number of fake sites in the load test.
TOTAL_SITES = 20

#: Expected number of sites that should be classified as SLOW.
EXPECTED_SLOW_COUNT = 5


# ──────────────────────────────────────────────────────────────────────
# Site list builder (pure — takes a mock port number)
# ──────────────────────────────────────────────────────────────────────


def build_sites(mock_port: int) -> list[dict[str, Any]]:
    """Build 20 fake sites pointing at the mock server on localhost.

    Five categories:
      • 5 fast       – /ok                         → OK
      • 5 slow – /slow?ms=6000                 → ≈6 s > 5000 ms → SLOW
      • 5 hang      – /hang                        → per‑site timeout (10 s) → CONNECT_TIMEOUT
      • 5 errors    – /status/500                  → HTTP_5XX
    """
    sites: list[dict[str, Any]] = []

    for i in range(5):
        sites.append(
            {
                "id": i + 1,
                "name": f"Fast Site {i + 1}",
                "url": f"http://127.0.0.1:{mock_port}/ok",
                "expected_status": 200,
                "keyword": None,
                "slow_threshold_ms": 5000,
                "enabled": True,
            }
        )

    for i in range(5):
        sites.append(
            {
                "id": 5 + i + 1,
                "name": f"Slow Site {i + 1}",
                "url": f"http://127.0.0.1:{mock_port}/slow?ms=6000",
                "expected_status": 200,
                "keyword": None,
                "slow_threshold_ms": 5000,
                "enabled": True,
            }
        )

    for i in range(5):
        sites.append(
            {
                "id": 10 + i + 1,
                "name": f"Hang Site {i + 1}",
                "url": f"http://127.0.0.1:{mock_port}/hang",
                "expected_status": 200,
                "keyword": None,
                "slow_threshold_ms": 5000,
                "enabled": True,
            }
        )

    for i in range(5):
        sites.append(
            {
                "id": 15 + i + 1,
                "name": f"Error Site {i + 1}",
                "url": f"http://127.0.0.1:{mock_port}/status/500",
                "expected_status": 200,
                "keyword": None,
                "slow_threshold_ms": 5000,
                "enabled": True,
            }
        )

    return sites


# ──────────────────────────────────────────────────────────────────────
# Check wrapper – forces the test-only allow_private flag for the mock server
# ──────────────────────────────────────────────────────────────────────


def check_url_with_private(
    url: str,
    slow_threshold_ms: int = 5000,
    cert_expiry_warning_days: int = 7,
    expected_status: int = 200,
    expected_keyword: str | None = None,
) -> CheckResult:
    """Run the real checker against the local mock server (SSRF bypassed)."""
    return check_url(
        url,
        slow_threshold_ms=slow_threshold_ms,
        cert_expiry_warning_days=cert_expiry_warning_days,
        expected_status=expected_status,
        expected_keyword=expected_keyword,
        allow_private=True,
    )


# ──────────────────────────────────────────────────────────────────────
# Summary helper (pure)
# ──────────────────────────────────────────────────────────────────────


def summarise(summary: dict[str, Any]) -> dict[str, Any]:
    """Extract the numbers we care about from the runner summary."""
    counts: dict[str, int] = summary.get("counts", {})
    checked: int = summary.get("sites_checked", 0)
    ok = counts.get("OK", 0)
    slow = counts.get("SLOW", 0)
    timeout = counts.get("CONNECT_TIMEOUT", 0)
    http_5xx = counts.get("HTTP_5XX", 0)
    other = checked - ok - slow - timeout - http_5xx
    return {
        "sites_checked": checked,
        "ok": ok,
        "slow": slow,
        "timeout": timeout,
        "http_5xx": http_5xx,
        "other": other,
    }


# ──────────────────────────────────────────────────────────────────────
# Pass / fail logic for T18
# ──────────────────────────────────────────────────────────────────────


def evaluate(summary: dict[str, Any], duration_ms: float) -> bool:
    """Return True when the run satisfies the T18 criteria.

    T18: 20 sites with 5 slow ones → run finishes within the time budget.
    """
    stats = summarise(summary)
    if stats["sites_checked"] != TOTAL_SITES:
        return False
    if stats["slow"] != EXPECTED_SLOW_COUNT:
        return False
    deadline_ms = config.runner.run_deadline_seconds * 1000
    return duration_ms < deadline_ms


# ──────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────


def main() -> int:
    """Run the Phase 6 part‑A load test and report PASS / FAIL against T18."""
    mock_server = MockServer()
    try:
        mock_port = mock_server.port
        sites = build_sites(mock_port)

        run_start = time.perf_counter()
        summary = run_all_checks(
            list_sites_func=lambda conn=None, enabled_only=True: sites if enabled_only else list(sites),
            check_url_func=check_url_with_private,
            save_check_func=lambda site_id, result, conn=None: 1,
            purge_func=lambda conn=None: 0,
            process_check_func=lambda conn, site, result: None,
            connect_func=lambda: None,
            max_workers=config.runner.max_workers,
            site_timeout_seconds=config.runner.site_timeout_seconds,
            run_deadline_seconds=config.runner.run_deadline_seconds,
        )
        duration_ms = (time.perf_counter() - run_start) * 1000

        stats = summarise(summary)

        passed = evaluate(summary, duration_ms)

        print(f"\n{'=' * 60}")
        print("WHYDOWN LOAD TEST — Phase 6 Part A")
        print(f"{'=' * 60}")
        print(f"\nTotal run time: {duration_ms:,.0f} ms  (deadline: {config.runner.run_deadline_seconds * 1000:.0f} ms)")
        print(f"\nSites checked: {stats['sites_checked']}")
        print(f"  OK:        {stats['ok']}")
        print(f"  SLOW:      {stats['slow']} (expected: {EXPECTED_SLOW_COUNT})")
        print(f"  TIMEOUT:   {stats['timeout']} (per-site timeout)")
        print(f"  HTTP_5XX:  {stats['http_5xx']}")
        print(f"  OTHER:     {stats['other']}")
        print("\nT18 Criteria:")
        print(f"  5 slow sites:           {'PASS' if stats['slow'] == EXPECTED_SLOW_COUNT else 'FAIL'}")
        print(f"  Run within {config.runner.run_deadline_seconds * 1000:.0f} ms budget: {'PASS' if duration_ms < config.runner.run_deadline_seconds * 1000 else 'FAIL'}")
        print()

        if passed:
            print("RESULT: PASS")
            return 0
        print("RESULT: FAIL")
        return 1

    finally:
        mock_server.stop()


if __name__ == "__main__":
    sys.exit(main())
