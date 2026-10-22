"""Tests for the incident handling inside the check runner (Phase 4 part A).

The runner feeds every stored check into the incident state machine, so an
incident opens and closes while the run goes on. These tests replace the
state machine with a recording fake: no database and no network is used.
The alert of an incident is replaced by a fake as well, so no message is
ever sent while the tests run.
"""

from __future__ import annotations

import pytest

from app import runner
from app.checker import CheckResult


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


def _result(code: str) -> CheckResult:
    result = CheckResult()
    result.result = code
    result.total_ms = 10.0
    return result


class FakeConnection:
    """A stand-in for a psycopg connection; the runner only closes it."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeRunnerEnv:
    """The fake database layer, check function and state machine of a run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.sites: list[dict[str, object]] = []
        self.codes: dict[str, str] = {}
        self.process_error: Exception | None = None
        self.outcomes: dict[str, str | None] = {"OK": None, "HTTP_5XX": "opened"}

    # -- the database layer ------------------------------------------------

    def list_sites(self, conn=None, enabled_only=False):
        self.calls.append(("list_sites", (conn,), {}))
        return list(self.sites)

    def save_check(self, site_id, result, conn=None):
        self.calls.append(("save_check", (site_id, result.result), {"conn": conn}))
        return 1

    def purge_old_checks(self, days=30, conn=None):
        self.calls.append(("purge_old_checks", (), {"conn": conn}))
        return 0

    def connect(self):
        return FakeConnection()

    # -- the check function ------------------------------------------------

    def check_url(self, url, **kwargs):
        return _result(self.codes.get(url, "OK"))

    # -- the incident state machine ----------------------------------------

    def process_check(self, conn, site, result):
        self.calls.append(("process_check", (site["id"], result.result), {"conn": conn}))
        if self.process_error is not None:
            raise self.process_error
        return self.outcomes.get(result.result)

    # -- the incident lookup and the alerts (Phase 4 part B) ---------------

    def get_latest_incident(self, site_id, conn=None):
        self.calls.append(("get_latest_incident", (site_id,), {"conn": conn}))
        return {
            "id": 1,
            "site_id": site_id,
            "started_at": None,
            "ended_at": None,
            "failure_type": "HTTP_5XX",
        }

    def send_alert(self, conn, site, incident, kind):
        self.calls.append(("send_alert", (site["id"], kind), {"conn": conn}))
        return True

    # -- helpers -----------------------------------------------------------

    def call_names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def call_args(self, name: str) -> list[tuple[object, ...]]:
        return [args for called, args, _ in self.calls if called == name]


@pytest.fixture
def env(monkeypatch):
    """Replace the database layer, the check function and the state machine."""
    fake = FakeRunnerEnv()
    monkeypatch.setattr(runner, "_connect", fake.connect)
    monkeypatch.setattr(runner, "list_sites", fake.list_sites)
    monkeypatch.setattr(runner, "save_check", fake.save_check)
    monkeypatch.setattr(runner, "purge_old_checks", fake.purge_old_checks)
    monkeypatch.setattr(runner, "check_url", fake.check_url)
    monkeypatch.setattr(runner, "process_check", fake.process_check)
    monkeypatch.setattr(runner, "get_latest_incident", fake.get_latest_incident)
    monkeypatch.setattr(runner, "send_alert", fake.send_alert)
    return fake


def test_the_state_machine_runs_after_every_saved_check(env):
    """Every stored check is handed to the incident state machine."""
    env.sites = [_make_site(1, "https://a.test"), _make_site(2, "https://b.test")]

    runner.run_all_checks(max_workers=2, site_timeout_seconds=5, run_deadline_seconds=20)

    assert env.call_names().count("process_check") == 2


def test_the_check_is_stored_before_the_state_machine_sees_it(env):
    """The state machine reads the check from the database, so it comes second."""
    env.sites = [_make_site(1, "https://a.test")]

    runner.run_all_checks(max_workers=1, site_timeout_seconds=5, run_deadline_seconds=20)

    names = env.call_names()
    assert names.index("save_check") < names.index("process_check")


def test_the_state_machine_gets_the_site_and_the_result(env):
    """The state machine is told which site the check belongs to."""
    env.sites = [_make_site(7, "https://a.test")]
    env.codes = {"https://a.test": "HTTP_5XX"}

    runner.run_all_checks(max_workers=1, site_timeout_seconds=5, run_deadline_seconds=20)

    assert env.call_args("process_check")[0][0] == 7
    assert env.call_args("process_check")[0][1] == "HTTP_5XX"


def test_the_state_machine_uses_the_connection_of_the_run(env):
    """The same connection is used for the check and for the incident."""
    env.sites = [_make_site(1, "https://a.test")]

    runner.run_all_checks(max_workers=1, site_timeout_seconds=5, run_deadline_seconds=20)

    save_conn = env.calls[0]
    save_conns = [kw["conn"] for name, _, kw in env.calls if name == "save_check"]
    process_conns = [kw["conn"] for name, _, kw in env.calls if name == "process_check"]
    assert save_conns[0] is process_conns[0]
    assert save_conn is not None


def test_a_failing_state_machine_does_not_stop_the_run(env):
    """An incident error of one site never stops the other sites."""
    env.sites = [_make_site(1, "https://a.test"), _make_site(2, "https://b.test")]
    env.process_error = RuntimeError("the incident state is broken")

    summary = runner.run_all_checks(
        max_workers=2, site_timeout_seconds=5, run_deadline_seconds=20
    )

    assert summary["sites_checked"] == 2
    assert summary["counts"] == {"OK": 2}
    # Both sites were still handed to the state machine.
    assert env.call_names().count("process_check") == 2


def test_a_failed_save_skips_the_state_machine(env, monkeypatch):
    """Without a stored check there is nothing for the state machine to read."""
    env.sites = [_make_site(1, "https://a.test")]

    def failing_save(site_id, result, conn=None):
        raise RuntimeError("write failed")

    monkeypatch.setattr(runner, "save_check", failing_save)

    runner.run_all_checks(max_workers=1, site_timeout_seconds=5, run_deadline_seconds=20)

    assert "process_check" not in env.call_names()
