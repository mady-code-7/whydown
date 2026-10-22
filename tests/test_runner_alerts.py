"""Tests for the alerts inside the check runner (Phase 4 part B).

The runner sends the DOWN alert when the incident state machine opens an
incident and the RECOVERY alert when it closes one. These tests replace the
state machine, the incident lookup and the alert sender with recording
fakes: no database and no network is used.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
    """The fake database layer, check function, state machine and alerts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.sites: list[dict[str, object]] = []
        self.codes: dict[str, str] = {}
        self.outcomes: dict[str, str | None] = {
            "HTTP_5XX": "opened",
            "RECOVERED": "closed",
            "OK": None,
        }
        self.alerts: list[tuple[int, str, dict[str, object]]] = []
        self.alert_error: Exception | None = None
        self.incident_error: Exception | None = None
        self.incident: dict[str, object] | None = None

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
        return self.outcomes.get(result.result)

    # -- the incident lookup and the alerts --------------------------------

    def get_latest_incident(self, site_id, conn=None):
        self.calls.append(("get_latest_incident", (site_id,), {"conn": conn}))
        if self.incident_error is not None:
            raise self.incident_error
        if self.incident is not None:
            return dict(self.incident)
        return {
            "id": 1,
            "site_id": site_id,
            "started_at": datetime(2026, 10, 22, 10, 0, 0, tzinfo=UTC),
            "ended_at": None,
            "failure_type": "HTTP_5XX",
        }

    def send_alert(self, conn, site, incident, kind):
        self.calls.append(("send_alert", (site["id"], kind), {"conn": conn}))
        if self.alert_error is not None:
            raise self.alert_error
        self.alerts.append((site["id"], kind, incident))
        return True

    # -- helpers -----------------------------------------------------------

    def call_names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def call_args(self, name: str) -> list[tuple[object, ...]]:
        return [args for called, args, _ in self.calls if called == name]


@pytest.fixture
def env(monkeypatch):
    """Replace the database layer, the state machine and the alerts."""
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


# --------------------------------------------------------------------------
# The alerts of the run
# --------------------------------------------------------------------------

def test_a_down_alert_is_sent_when_an_incident_opens(env):
    """An opened incident sends a DOWN alert."""
    env.sites = [_make_site(1, "https://a.test")]
    env.codes = {"https://a.test": "HTTP_5XX"}

    runner.run_all_checks(max_workers=1, site_timeout_seconds=5, run_deadline_seconds=20)

    assert [(site_id, kind) for site_id, kind, _ in env.alerts] == [(1, "DOWN")]


def test_a_recovery_alert_is_sent_when_an_incident_closes(env):
    """A closed incident sends a RECOVERY alert."""
    env.sites = [_make_site(1, "https://a.test")]
    env.codes = {"https://a.test": "RECOVERED"}

    runner.run_all_checks(max_workers=1, site_timeout_seconds=5, run_deadline_seconds=20)

    assert [(site_id, kind) for site_id, kind, _ in env.alerts] == [(1, "RECOVERY")]


def test_no_alert_is_sent_when_nothing_changed(env):
    """A check that changes nothing sends no alert."""
    env.sites = [_make_site(1, "https://a.test")]
    env.codes = {"https://a.test": "OK"}

    runner.run_all_checks(max_workers=1, site_timeout_seconds=5, run_deadline_seconds=20)

    assert env.alerts == []


def test_the_alert_gets_the_incident_of_the_site(env):
    """The incident that was read from the database is handed to the alert."""
    env.sites = [_make_site(4, "https://a.test")]
    env.codes = {"https://a.test": "HTTP_5XX"}
    env.incident = {"id": 42, "site_id": 4, "failure_type": "HTTP_5XX"}

    runner.run_all_checks(max_workers=1, site_timeout_seconds=5, run_deadline_seconds=20)

    assert env.alerts[0][2]["id"] == 42


def test_the_alert_uses_the_connection_of_the_run(env):
    """The alert uses the same connection as the check."""
    env.sites = [_make_site(1, "https://a.test")]
    env.codes = {"https://a.test": "HTTP_5XX"}

    runner.run_all_checks(max_workers=1, site_timeout_seconds=5, run_deadline_seconds=20)

    save_conns = [kw["conn"] for name, _, kw in env.calls if name == "save_check"]
    alert_conns = [kw["conn"] for name, _, kw in env.calls if name == "send_alert"]
    assert alert_conns[0] is save_conns[0]


# --------------------------------------------------------------------------
# A failing alert never stops the run
# --------------------------------------------------------------------------

def test_a_failing_alert_does_not_stop_the_run(env):
    """A broken alert channel never stops the other sites."""
    env.sites = [_make_site(1, "https://a.test"), _make_site(2, "https://b.test")]
    env.codes = {"https://a.test": "HTTP_5XX", "https://b.test": "OK"}
    env.alert_error = RuntimeError("the alert channel is down")

    summary = runner.run_all_checks(
        max_workers=2, site_timeout_seconds=5, run_deadline_seconds=20
    )

    assert summary["sites_checked"] == 2
    assert summary["counts"] == {"HTTP_5XX": 1, "OK": 1}


def test_a_failing_incident_lookup_does_not_stop_the_run(env):
    """A database error while looking for the incident never stops the run."""
    env.sites = [_make_site(1, "https://a.test"), _make_site(2, "https://b.test")]
    env.codes = {"https://a.test": "HTTP_5XX"}
    env.incident_error = RuntimeError("the database is gone")

    summary = runner.run_all_checks(
        max_workers=2, site_timeout_seconds=5, run_deadline_seconds=20
    )

    assert summary["sites_checked"] == 2
    assert env.alerts == []


def test_a_missing_incident_does_not_stop_the_run(env, monkeypatch):
    """An incident that cannot be found is skipped, the run continues."""
    env.sites = [_make_site(1, "https://a.test"), _make_site(2, "https://b.test")]
    env.codes = {"https://a.test": "HTTP_5XX"}

    monkeypatch.setattr(runner, "get_latest_incident", lambda site_id, conn=None: None)

    summary = runner.run_all_checks(
        max_workers=2, site_timeout_seconds=5, run_deadline_seconds=20
    )

    assert summary["sites_checked"] == 2
    assert env.alerts == []
