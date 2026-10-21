"""Tests for the check runner (Phase 3B-1).

No real database and no network access is used: the connection helper, the
database functions and ``check_url`` are all replaced by fakes. The runner
itself is pure orchestration, so a fake ``check_url`` drives every behaviour.

The fakes also record which connection every database call received, so a
test can prove that a run uses exactly one connection and that it is closed
again at the end.
"""

from __future__ import annotations

import time

import pytest

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


class FakeConnection:
    """A stand-in for a psycopg connection.

    The runner only opens and closes a connection, so nothing else is
    implemented. ``closed`` shows whether the runner gave it back.
    """

    def __init__(self, label: str = "conn") -> None:
        self.label = label
        self.closed = False
        self.close_error: Exception | None = None

    def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error
        self.closed = True

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"FakeConnection({self.label!r})"


class FakeEnvironment:
    """A fake database layer and check function for the runner.

    It records every database call together with the connection it received,
    so a test can check that one connection is used for the whole run.
    """

    def __init__(self) -> None:
        self.sites: list[dict[str, object]] = []
        self.codes: dict[str, str] = {}
        self.calls: list[tuple[str, object]] = []
        self.saved: dict[int, str] = {}
        self.connections: list[FakeConnection] = []
        self.connect_attempts = 0
        self.connect_error: Exception | None = None
        self.save_errors: dict[int, Exception] = {}
        self.purge_error: Exception | None = None
        self.check_error: Exception | None = None
        self.check_delay = 0.0
        self.delays: dict[str, float] = {}
        self.purges = 0

    # -- the database layer ------------------------------------------------

    def connect(self) -> FakeConnection:
        """Open a connection (or fail like a database that is not there)."""
        self.connect_attempts += 1
        if self.connect_error is not None:
            raise self.connect_error
        conn = FakeConnection(f"conn{len(self.connections) + 1}")
        self.connections.append(conn)
        return conn

    def list_sites(self, conn=None, enabled_only=False):
        self.calls.append(("list_sites", conn))
        return list(self.sites)

    def save_check(self, site_id, result, conn=None):
        self.calls.append(("save_check", conn))
        error = self.save_errors.get(site_id)
        if error is not None:
            raise error
        self.saved[site_id] = result.result
        return len(self.saved)

    def purge_old_checks(self, days=30, conn=None):
        self.calls.append(("purge_old_checks", conn))
        self.purges += 1
        if self.purge_error is not None:
            raise self.purge_error
        return 0

    # -- the check function ------------------------------------------------

    def check_url(self, url, **kwargs):
        delay = self.delays.get(url, self.check_delay)
        if delay:
            time.sleep(delay)
        if self.check_error is not None:
            raise self.check_error
        return _result(self.codes.get(url, "OK"))

    # -- helpers -----------------------------------------------------------

    def connections_of(self, call: str) -> list[object]:
        """Every connection the given database call received."""
        return [conn for name, conn in self.calls if name == call]


@pytest.fixture
def env(monkeypatch) -> FakeEnvironment:
    """Replace the database layer and the check function of the runner."""
    fake = FakeEnvironment()
    monkeypatch.setattr(runner, "_connect", fake.connect)
    monkeypatch.setattr(runner, "list_sites", fake.list_sites)
    monkeypatch.setattr(runner, "save_check", fake.save_check)
    monkeypatch.setattr(runner, "purge_old_checks", fake.purge_old_checks)
    monkeypatch.setattr(runner, "check_url", fake.check_url)
    return fake


# --------------------------------------------------------------------------
# run_all_checks summary
# --------------------------------------------------------------------------

def test_summary_contains_counts_and_duration(env):
    """run_all_checks returns sites_checked, counts per code and duration_ms."""
    env.sites = [_make_site(1, "https://a.test"), _make_site(2, "https://b.test")]
    env.codes = {"https://a.test": "OK", "https://b.test": "SLOW"}

    summary = runner.run_all_checks(max_workers=2, site_timeout_seconds=5, run_deadline_seconds=20)

    assert summary["sites_checked"] == 2
    assert summary["counts"] == {"OK": 1, "SLOW": 1}
    assert env.saved == {1: "OK", 2: "SLOW"}
    assert isinstance(summary["duration_ms"], int)


def test_empty_site_list_returns_empty_summary_and_still_purges(env):
    """With no sites the run still calls purge_old_checks once."""
    env.sites = []

    summary = runner.run_all_checks()

    assert summary["sites_checked"] == 0
    assert summary["counts"] == {}
    assert env.purges == 1  # purge called exactly once


# --------------------------------------------------------------------------
# every result saved
# --------------------------------------------------------------------------

def test_every_result_is_saved_in_order(env):
    """save_check is called once per site with the check's classification."""
    env.sites = [_make_site(1, "https://a.test"), _make_site(2, "https://b.test")]
    env.codes = {"https://a.test": "DNS_FAILURE", "https://b.test": "HTTP_5XX"}

    runner.run_all_checks(max_workers=2, site_timeout_seconds=5, run_deadline_seconds=20)

    assert env.saved == {1: "DNS_FAILURE", 2: "HTTP_5XX"}


# --------------------------------------------------------------------------
# per‑site timeout with a fake slow check
# --------------------------------------------------------------------------

def test_site_timeout_is_saved_as_timeout_and_counted(env):
    """A site that does not finish within SITE_TIMEOUT_SECONDS becomes a timeout."""
    env.sites = [_make_site(1, "https://slow.test")]
    env.check_delay = 1.0

    summary = runner.run_all_checks(max_workers=1, site_timeout_seconds=0.2, run_deadline_seconds=20)

    assert summary["sites_checked"] == 1
    assert env.saved == {1: "CONNECT_TIMEOUT"}
    assert summary["counts"] == {"CONNECT_TIMEOUT": 1}


# --------------------------------------------------------------------------
# one failing site does not stop the rest
# --------------------------------------------------------------------------

def test_one_failing_site_does_not_stop_the_others(env, monkeypatch):
    """A check that raises (or fails) is recorded as INTERNAL_ERROR; the rest run."""
    env.sites = [
        _make_site(1, "https://good.test"),
        _make_site(2, "https://failing.test"),
        _make_site(3, "https://good2.test"),
    ]

    def mixed_check(url, **kwargs):
        if "failing" in url:
            raise RuntimeError("boom")
        return _result("OK")

    monkeypatch.setattr(runner, "check_url", mixed_check)

    summary = runner.run_all_checks(max_workers=2, site_timeout_seconds=5, run_deadline_seconds=20)

    assert summary["sites_checked"] == 3
    assert env.saved == {1: "OK", 2: "INTERNAL_ERROR", 3: "OK"}


# --------------------------------------------------------------------------
# purge called once
# --------------------------------------------------------------------------

def test_purge_called_once(env):
    """purge_old_checks is called exactly once at the end of a run."""
    env.sites = [_make_site(1, "https://a.test")]

    runner.run_all_checks()

    assert env.purges == 1
    assert [name for name, _ in env.calls].count("purge_old_checks") == 1


# --------------------------------------------------------------------------
# run deadline
# --------------------------------------------------------------------------

def test_run_deadline_saves_unfinished_sites_as_timeouts(env):
    """Sites not finished by RUN_DEADLINE_SECONDS are saved as timeouts."""
    env.sites = [_make_site(1, "https://slow.test")]
    env.check_delay = 1.0

    summary = runner.run_all_checks(max_workers=1, site_timeout_seconds=5.0, run_deadline_seconds=0.3)

    assert summary["sites_checked"] == 1
    assert env.saved == {1: "CONNECT_TIMEOUT"}
    assert summary["counts"] == {"CONNECT_TIMEOUT": 1}


# --------------------------------------------------------------------------
# one connection per run
# --------------------------------------------------------------------------

def test_only_one_connection_is_opened_per_run(env):
    """A run opens exactly one connection, however many sites it checks."""
    env.sites = [_make_site(1, "https://a.test"), _make_site(2, "https://b.test")]

    runner.run_all_checks(max_workers=2, site_timeout_seconds=5, run_deadline_seconds=20)

    assert env.connect_attempts == 1
    assert len(env.connections) == 1


def test_every_database_call_uses_the_same_connection(env):
    """list_sites, save_check and purge_old_checks all get that one connection."""
    env.sites = [_make_site(1, "https://a.test"), _make_site(2, "https://b.test")]

    runner.run_all_checks(max_workers=2, site_timeout_seconds=5, run_deadline_seconds=20)

    conn = env.connections[0]
    assert env.connections_of("list_sites") == [conn]
    assert env.connections_of("save_check") == [conn, conn]
    assert env.connections_of("purge_old_checks") == [conn]


def test_the_connection_is_closed_at_the_end_of_the_run(env):
    """The connection of a finished run is closed again."""
    env.sites = [_make_site(1, "https://a.test")]

    runner.run_all_checks()

    assert env.connections[0].closed is True


def test_the_connection_is_closed_when_the_run_fails(env, monkeypatch):
    """A failing run still gives the connection back."""
    env.sites = [_make_site(1, "https://a.test")]

    def boom(conn=None, enabled_only=False):
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(runner, "list_sites", boom)

    with pytest.raises(RuntimeError):
        runner.run_all_checks()

    assert env.connections[0].closed is True


def test_failing_to_open_the_connection_raises(env):
    """Without a connection the run cannot do anything, so the error travels on."""
    env.connect_error = RuntimeError("no database today")
    env.sites = [_make_site(1, "https://a.test")]

    with pytest.raises(RuntimeError):
        runner.run_all_checks()

    # No site was read and nothing was stored.
    assert env.calls == []
    assert env.saved == {}


# --------------------------------------------------------------------------
# a failed save
# --------------------------------------------------------------------------

def test_a_failed_save_is_counted_and_the_connection_is_reopened_once(env):
    """One failed save: one reconnect, one save_error, the run continues."""
    env.sites = [_make_site(1, "https://a.test")]
    env.save_errors = {1: RuntimeError("write failed")}

    summary = runner.run_all_checks()

    assert summary["save_errors"] == 1
    assert summary["sites_checked"] == 1
    assert env.connect_attempts == 2  # the initial one plus one reopen
    # The first connection is closed, the second one is used from then on.
    assert env.connections[0].closed is True
    assert env.connections[1].closed is True
    assert env.connections_of("purge_old_checks") == [env.connections[1]]


def test_the_run_continues_after_a_failed_save(env):
    """A failed save does not stop the run: the other site is still checked."""
    env.sites = [_make_site(1, "https://a.test"), _make_site(2, "https://b.test")]
    env.save_errors = {1: RuntimeError("write failed")}

    summary = runner.run_all_checks(max_workers=2, site_timeout_seconds=5, run_deadline_seconds=20)

    assert summary["sites_checked"] == 2
    assert summary["save_errors"] == 1
    # The site whose save failed is missing, the other one was stored.
    assert env.saved == {2: "OK"}
    # One reconnect, and the run finished with the new connection.
    assert env.connect_attempts == 2
    assert env.connections_of("purge_old_checks") == [env.connections[1]]


def test_the_reopened_connection_is_used_for_the_next_save(env):
    """After a failed save the next save uses the reopened connection."""
    env.sites = [_make_site(1, "https://a.test"), _make_site(2, "https://slow.test")]
    env.save_errors = {1: RuntimeError("write failed")}
    # Site 2 answers too late and is recorded as a timeout. It is collected in
    # a later pass than site 1, so the order of the two saves is fixed.
    env.delays = {"https://slow.test": 1.0}

    summary = runner.run_all_checks(
        max_workers=2, site_timeout_seconds=0.2, run_deadline_seconds=20
    )

    assert summary["save_errors"] == 1
    assert env.connections_of("save_check") == [
        env.connections[0],
        env.connections[1],
    ]
    assert env.saved == {2: "CONNECT_TIMEOUT"}


def test_save_errors_are_only_reported_when_above_zero(env):
    """A clean run has no save_errors entry in its summary."""
    env.sites = [_make_site(1, "https://a.test")]

    summary = runner.run_all_checks()

    assert "save_errors" not in summary


def test_a_failing_reopen_keeps_the_run_going(env, monkeypatch):
    """When the connection cannot be reopened the run still finishes."""
    env.sites = [_make_site(1, "https://a.test")]
    env.save_errors = {1: RuntimeError("write failed")}

    original_connect = env.connect
    attempts = {"count": 0}

    def failing_reconnect():
        attempts["count"] += 1
        if attempts["count"] > 1:
            raise RuntimeError("still no database")
        return original_connect()

    monkeypatch.setattr(runner, "_connect", failing_reconnect)

    summary = runner.run_all_checks()

    assert summary["save_errors"] == 1
    assert summary["sites_checked"] == 1
    assert env.connections[0].closed is True


def test_a_failing_purge_does_not_stop_the_run(env):
    """A purge that fails is logged; the run still reports its summary."""
    env.sites = [_make_site(1, "https://a.test")]
    env.purge_error = RuntimeError("delete failed")

    summary = runner.run_all_checks()

    assert summary["sites_checked"] == 1
    assert summary["counts"] == {"OK": 1}
    assert env.purges == 1
    assert env.connections[0].closed is True


def test_a_failing_close_does_not_stop_the_run(env, monkeypatch):
    """A connection that cannot be closed does not break the run."""
    env.sites = [_make_site(1, "https://a.test")]

    original_connect = env.connect

    def connect_with_bad_close():
        conn = original_connect()
        conn.close_error = RuntimeError("close failed")
        return conn

    monkeypatch.setattr(runner, "_connect", connect_with_bad_close)

    summary = runner.run_all_checks()

    assert summary["sites_checked"] == 1
    assert summary["counts"] == {"OK": 1}


def test_a_check_that_raises_is_recorded_as_an_internal_error(env, monkeypatch):
    """A future that raises is stored as INTERNAL_ERROR, not lost."""
    env.sites = [_make_site(1, "https://a.test")]

    def raising_check_site(site, check_fn):
        raise RuntimeError("the check exploded")

    monkeypatch.setattr(runner, "_check_site", raising_check_site)

    summary = runner.run_all_checks()

    assert summary["sites_checked"] == 1
    assert env.saved == {1: "INTERNAL_ERROR"}
    assert summary["counts"] == {"INTERNAL_ERROR": 1}


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
