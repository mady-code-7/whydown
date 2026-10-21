"""Tests for the check runner (Phase 3B-1).

No real database and no network access is used: ``check_url``, ``list_sites``,
``save_check`` and ``purge_old_checks`` are all mocked. The runner itself is
pure orchestration, so a fake ``check_url`` drives every behaviour.
"""

from __future__ import annotations

import time

from app import runner
from app.checker import CheckResult
from app.config import load_config


def _make_site(site_id: int, url: str) -> dict[str, object]:
    return {
        "id": site_id,
        "name": f"Site {site_id}",
        "url": url,
        "expected_status": 200,
        "keyword": None,
        "slow_threshold_ms": 5000,
        "enabled": True,
    }


def _result(code: str, total_ms: float = 10.0) -> CheckResult:
    result = CheckResult()
    result.result = code
    result.failed_stage = None
    result.total_ms = total_ms
    return result


# --------------------------------------------------------------------------
# run_all_checks summary
# --------------------------------------------------------------------------

def test_summary_contains_counts_and_duration(monkeypatch):
    """run_all_checks returns sites_checked, counts per code and duration_ms."""
    sites = [_make_site(1, "https://a.test"), _make_site(2, "https://b.test")]
    codes_by_site = {1: "OK", 2: "SLOW"}
    saved = {}

    monkeypatch.setattr(runner, "list_sites", lambda enabled_only=True: sites)
    monkeypatch.setattr(
        runner,
        "check_url",
        lambda url, **kwargs: _result(codes_by_site[1] if "a" in url else codes_by_site[2]),
    )
    monkeypatch.setattr(
        runner, "save_check",
        lambda site_id, res: saved.__setitem__(site_id, res.result),
    )
    monkeypatch.setattr(runner, "purge_old_checks", lambda: 0)

    summary = runner.run_all_checks(max_workers=2, site_timeout_seconds=5, run_deadline_seconds=20)

    assert summary["sites_checked"] == 2
    assert summary["counts"] == {"OK": 1, "SLOW": 1}
    assert saved == {1: "OK", 2: "SLOW"}
    assert isinstance(summary["duration_ms"], int)


def test_empty_site_list_returns_empty_summary_and_still_purges(monkeypatch):
    """With no sites the run still calls purge_old_checks once."""
    monkeypatch.setattr(runner, "list_sites", lambda enabled_only=True: [])
    purged = []
    monkeypatch.setattr(runner, "purge_old_checks", lambda: purged.append(1) or 0)

    summary = runner.run_all_checks()

    assert summary["sites_checked"] == 0
    assert summary["counts"] == {}
    assert purged == [1]  # purge called exactly once


# --------------------------------------------------------------------------
# every result saved
# --------------------------------------------------------------------------

def test_every_result_is_saved_in_order(monkeypatch):
    """save_check is called once per site with the check's classification."""
    sites = [_make_site(1, "https://a.test"), _make_site(2, "https://b.test")]
    codes_by_site = {1: "DNS_FAILURE", 2: "HTTP_5XX"}
    saved = {}

    monkeypatch.setattr(runner, "list_sites", lambda enabled_only=True: sites)
    monkeypatch.setattr(
        runner,
        "check_url",
        lambda url, **kwargs: _result(codes_by_site[1] if "a" in url else codes_by_site[2]),
    )
    monkeypatch.setattr(
        runner, "save_check",
        lambda site_id, res: saved.__setitem__(site_id, res.result),
    )
    monkeypatch.setattr(runner, "purge_old_checks", lambda: 0)

    runner.run_all_checks(max_workers=2, site_timeout_seconds=5, run_deadline_seconds=20)

    assert saved == {1: "DNS_FAILURE", 2: "HTTP_5XX"}


# --------------------------------------------------------------------------
# per‑site timeout with a fake slow check
# --------------------------------------------------------------------------

def test_site_timeout_is_saved_as_timeout_and_counted(monkeypatch):
    """A site that does not finish within SITE_TIMEOUT_SECONDS becomes a timeout."""
    def slow_check(url, **kwargs):
        time.sleep(1.0)
        return _result("OK")

    monkeypatch.setattr(
        runner,
        "list_sites",
        lambda enabled_only=True: [_make_site(1, "https://slow.test")],
    )
    monkeypatch.setattr(runner, "check_url", slow_check)
    saved = []
    monkeypatch.setattr(runner, "save_check", lambda site_id, res: saved.append(res.result))
    monkeypatch.setattr(runner, "purge_old_checks", lambda: 0)

    summary = runner.run_all_checks(max_workers=1, site_timeout_seconds=0.2, run_deadline_seconds=20)

    assert summary["sites_checked"] == 1
    assert saved == ["CONNECT_TIMEOUT"]
    assert summary["counts"] == {"CONNECT_TIMEOUT": 1}


# --------------------------------------------------------------------------
# one failing site does not stop the rest
# --------------------------------------------------------------------------

def test_one_failing_site_does_not_stop_the_others(monkeypatch):
    """A check that raises (or fails) is recorded as INTERNAL_ERROR; the rest run."""
    def mixed_check(url, **kwargs):
        if "failing" in url:
            raise RuntimeError("boom")
        return _result("OK")

    sites = [
        _make_site(1, "https://good.test"),
        _make_site(2, "https://failing.test"),
        _make_site(3, "https://good2.test"),
    ]
    monkeypatch.setattr(runner, "list_sites", lambda enabled_only=True: sites)
    monkeypatch.setattr(runner, "check_url", mixed_check)
    saved = []
    monkeypatch.setattr(runner, "save_check", lambda site_id, res: saved.append((site_id, res.result)))
    monkeypatch.setattr(runner, "purge_old_checks", lambda: 0)

    summary = runner.run_all_checks(max_workers=2, site_timeout_seconds=5, run_deadline_seconds=20)

    assert summary["sites_checked"] == 3
    codes = {site_id: code for site_id, code in saved}
    assert codes[1] == "OK"
    assert codes[3] == "OK"
    assert codes[2] == "INTERNAL_ERROR"


# --------------------------------------------------------------------------
# purge called once
# --------------------------------------------------------------------------

def test_purge_called_once(monkeypatch):
    """purge_old_checks is called exactly once at the end of a run."""
    sites = [_make_site(1, "https://a.test")]
    monkeypatch.setattr(runner, "list_sites", lambda enabled_only=True: sites)
    monkeypatch.setattr(runner, "check_url", lambda url, **kwargs: _result("OK"))
    calls = []
    monkeypatch.setattr(runner, "save_check", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(runner, "purge_old_checks", lambda: calls.append("purge") or 0)

    runner.run_all_checks()

    assert calls.count("purge") == 1


# --------------------------------------------------------------------------
# run deadline
# --------------------------------------------------------------------------

def test_run_deadline_saves_unfinished_sites_as_timeouts(monkeypatch):
    """Sites not finished by RUN_DEADLINE_SECONDS are saved as timeouts."""
    def slow_check(url, **kwargs):
        time.sleep(1.0)
        return _result("OK")

    sites = [_make_site(1, "https://slow.test")]
    monkeypatch.setattr(runner, "list_sites", lambda enabled_only=True: sites)
    monkeypatch.setattr(runner, "check_url", slow_check)
    saved = []
    monkeypatch.setattr(runner, "save_check", lambda site_id, res: saved.append(res.result))
    monkeypatch.setattr(runner, "purge_old_checks", lambda: 0)

    summary = runner.run_all_checks(max_workers=1, site_timeout_seconds=5.0, run_deadline_seconds=0.3)

    assert summary["sites_checked"] == 1
    assert saved == ["CONNECT_TIMEOUT"]
    assert summary["counts"] == {"CONNECT_TIMEOUT": 1}


# --------------------------------------------------------------------------
# config defaults and bad values
# --------------------------------------------------------------------------

def test_config_defaults(monkeypatch):
    """The runner config defaults to MAX_WORKERS=8, SITE_TIMEOUT_SECONDS=10, RUN_DEADLINE_SECONDS=25."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/testdb")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    monkeypatch.delenv("MAX_WORKERS", raising=False)
    monkeypatch.delenv("SITE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("RUN_DEADLINE_SECONDS", raising=False)

    cfg = load_config()

    assert cfg.runner.max_workers == 8
    assert cfg.runner.site_timeout_seconds == 10
    assert cfg.runner.run_deadline_seconds == 25


def test_config_accepts_custom_values(monkeypatch):
    """Runner config values are read from the environment."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/testdb")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("MAX_WORKERS", "16")
    monkeypatch.setenv("SITE_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("RUN_DEADLINE_SECONDS", "30")

    cfg = load_config()

    assert cfg.runner.max_workers == 16
    assert cfg.runner.site_timeout_seconds == 15
    assert cfg.runner.run_deadline_seconds == 30


def test_config_rejects_bad_runner_values(monkeypatch):
    """Semantically bad runner values are rejected by validate()."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/testdb")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("MAX_WORKERS", "0")
    monkeypatch.setenv("RUN_DEADLINE_SECONDS", "5")

    cfg = load_config()

    errors = cfg.validate()
    assert "MAX_WORKERS must be at least 1" in errors
    assert "RUN_DEADLINE_SECONDS must be greater than SITE_TIMEOUT_SECONDS" in errors
