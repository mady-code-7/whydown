"""Tests for the incident state machine (Phase 4 part A).

``app.incidents.process_check`` reacts to the result of one check that was
just stored:

* an up result while no incident is open does nothing;
* an up result while an incident is open closes that incident;
* a failure accumulates consecutive failures - reaching the threshold opens
  one incident, and every further failure is ignored while the incident stays
  open.

The state of the machine lives in the database, never in memory, so these
tests replace the four database calls with a recording fake and drive the
machine one check at a time.
"""

from __future__ import annotations

import pytest

from app import incidents

# --------------------------------------------------------------------------
# The fake database layer
# --------------------------------------------------------------------------

class FakeIncidentStore:
    """A recording stand-in for the database calls of the state machine.

    It keeps the check results of every site in memory, so the fake answers
    "which are the recent results of this site" exactly like the database
    would after the check was stored.
    """

    def __init__(self) -> None:
        self.checks: dict[int, list[str]] = {}
        self.open_incidents: dict[int, dict[str, object]] = {}
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.opened: list[dict[str, object]] = []
        self.closed: list[int] = []
        self.errors: set[str] = set()
        self.next_incident_id = 1

    # -- helpers used by the tests -----------------------------------------

    def store_check(self, site_id: int, code: str) -> None:
        """Store the result code of a check, newest last (like the database)."""
        self.checks.setdefault(site_id, []).append(code)

    def call_names(self) -> list[str]:
        """The names of the database calls, in order."""
        return [name for name, _, _ in self.calls]

    def calls_of(self, name: str) -> list[tuple[object, ...]]:
        """The positional arguments of every call to ``name``."""
        return [args for called, args, _ in self.calls if called == name]

    # -- the four database calls -------------------------------------------

    def get_open_incident(self, site_id, conn=None):
        """Return the open incident of a site, or None."""
        self.calls.append(("get_open_incident", (site_id,), {"conn": conn}))
        if "get_open_incident" in self.errors:
            raise RuntimeError("the database is not there")
        return self.open_incidents.get(site_id)

    def open_incident(self, site_id, failure_type, consecutive_failures=1, conn=None):
        """Open a new incident for a site."""
        self.calls.append((
            "open_incident",
            (site_id, failure_type),
            {"consecutive_failures": consecutive_failures, "conn": conn},
        ))
        if "open_incident" in self.errors:
            raise RuntimeError("the database is not there")
        incident = {
            "id": self.next_incident_id,
            "site_id": site_id,
            "failure_type": failure_type,
            "consecutive_failures": consecutive_failures,
        }
        self.next_incident_id += 1
        self.open_incidents[site_id] = incident
        self.opened.append(incident)
        return incident["id"]

    def close_incident(self, incident_id, conn=None):
        """Close an open incident."""
        self.calls.append(("close_incident", (incident_id,), {"conn": conn}))
        if "close_incident" in self.errors:
            raise RuntimeError("the database is not there")
        for site_id, incident in list(self.open_incidents.items()):
            if incident["id"] == incident_id:
                del self.open_incidents[site_id]
                self.closed.append(incident_id)
                return True
        return False

    def recent_results(self, site_id, limit=20, conn=None):
        """Return the result codes of a site, newest first."""
        self.calls.append(("recent_results", (site_id,), {"limit": limit, "conn": conn}))
        if "recent_results" in self.errors:
            raise RuntimeError("the database is not there")
        codes = self.checks.get(site_id, [])
        return list(reversed(codes[-limit:]))


@pytest.fixture
def store(monkeypatch):
    """Replace the database calls of the state machine with the fake."""
    fake = FakeIncidentStore()
    monkeypatch.setattr(incidents, "get_open_incident", fake.get_open_incident)
    monkeypatch.setattr(incidents, "open_incident", fake.open_incident)
    monkeypatch.setattr(incidents, "close_incident", fake.close_incident)
    monkeypatch.setattr(incidents, "recent_results", fake.recent_results)
    # The threshold is pinned, so the tests do not depend on the environment.
    monkeypatch.setattr(incidents, "config", _config_with_threshold(3))
    return fake


def _config_with_threshold(threshold: int):
    """A stand-in for the app config with a fixed incident threshold."""
    from types import SimpleNamespace

    return SimpleNamespace(
        thresholds=SimpleNamespace(incident_threshold=threshold),
    )


def _site(site_id: int) -> dict[str, object]:
    """A minimal site row with the field the state machine needs."""
    return {"id": site_id}


def _result(code: str) -> object:
    """A minimal check result carrying the result code."""
    result = type("FakeResult", (), {})()
    result.result = code
    return result


# --------------------------------------------------------------------------
# Up results
# --------------------------------------------------------------------------

def test_up_result_with_no_open_incident_does_nothing(store):
    """A clean up check leaves the state machine alone."""
    store.store_check(1, "OK")

    assert incidents.process_check(None, _site(1), _result("OK")) is None

    assert "open_incident" not in store.call_names()
    assert "close_incident" not in store.call_names()


def test_up_result_while_an_incident_is_open_closes_it(store):
    """The first up result after an incident closes that incident."""
    store.open_incidents[1] = {"id": 7, "site_id": 1}
    store.store_check(1, "OK")

    assert incidents.process_check(None, _site(1), _result("OK")) == "closed"

    assert store.closed == [7]
    assert store.opened == []


def test_slow_and_dns_slow_count_as_up(store):
    """A slow but working site is up, so it closes an incident."""
    store.open_incidents[1] = {"id": 3, "site_id": 1}

    assert incidents.process_check(None, _site(1), _result("SLOW")) == "closed"

    assert store.closed == [3]


# --------------------------------------------------------------------------
# Failure accumulation
# --------------------------------------------------------------------------

def test_a_single_failure_opens_no_incident(store):
    """One failure is not enough to open an incident."""
    store.store_check(1, "DNS_FAILURE")

    assert incidents.process_check(None, _site(1), _result("DNS_FAILURE")) is None

    assert store.opened == []
    assert store.closed == []


def test_failures_below_the_threshold_open_no_incident(store):
    """Two failures stay below the threshold of three."""
    store.store_check(1, "DNS_FAILURE")
    store.store_check(1, "DNS_FAILURE")

    assert incidents.process_check(None, _site(1), _result("DNS_FAILURE")) is None

    assert store.opened == []


def test_exactly_n_failures_open_one_incident(store):
    """The third consecutive failure opens the incident."""
    for _ in range(3):
        store.store_check(1, "HTTP_5XX")

    assert incidents.process_check(None, _site(1), _result("HTTP_5XX")) == "opened"

    assert len(store.opened) == 1
    incident = store.opened[0]
    assert incident["site_id"] == 1
    assert incident["failure_type"] == "HTTP_5XX"
    assert incident["consecutive_failures"] == 3
    assert store.closed == []


def test_more_than_n_failures_open_only_one_incident(store):
    """Four failures still produce exactly one incident."""
    for _ in range(4):
        store.store_check(1, "READ_TIMEOUT")

    assert incidents.process_check(None, _site(1), _result("READ_TIMEOUT")) == "opened"

    assert len(store.opened) == 1
    assert store.opened[0]["consecutive_failures"] == 4


def test_the_incident_records_the_latest_failure_type(store):
    """The failure type of the incident is the code of the latest check."""
    store.store_check(1, "DNS_FAILURE")
    store.store_check(1, "DNS_FAILURE")
    store.store_check(1, "CONNECT_TIMEOUT")

    incidents.process_check(None, _site(1), _result("CONNECT_TIMEOUT"))

    assert store.opened[0]["failure_type"] == "CONNECT_TIMEOUT"


def test_the_failure_counter_stops_at_an_up_result(store):
    """An up result in the middle resets the counter."""
    store.store_check(1, "DNS_FAILURE")
    store.store_check(1, "DNS_FAILURE")
    store.store_check(1, "OK")
    store.store_check(1, "DNS_FAILURE")

    assert incidents.process_check(None, _site(1), _result("DNS_FAILURE")) is None

    assert store.opened == []


def test_the_threshold_comes_from_the_config(store, monkeypatch):
    """A threshold of one opens an incident at the first failure."""
    monkeypatch.setattr(incidents, "config", _config_with_threshold(1))
    store.store_check(1, "HTTP_5XX")

    assert incidents.process_check(None, _site(1), _result("HTTP_5XX")) == "opened"

    assert len(store.opened) == 1


def test_a_higher_threshold_needs_more_failures(store, monkeypatch):
    """A threshold of five needs five failures."""
    monkeypatch.setattr(incidents, "config", _config_with_threshold(5))
    for _ in range(4):
        store.store_check(1, "HTTP_5XX")

    assert incidents.process_check(None, _site(1), _result("HTTP_5XX")) is None

    store.store_check(1, "HTTP_5XX")
    assert incidents.process_check(None, _site(1), _result("HTTP_5XX")) == "opened"


# --------------------------------------------------------------------------
# Recovery and flapping
# --------------------------------------------------------------------------

def test_the_first_up_result_after_an_incident_closes_it(store):
    """The recovery closes the incident and reports it."""
    for _ in range(3):
        store.store_check(1, "HTTP_5XX")
    incidents.process_check(None, _site(1), _result("HTTP_5XX"))

    store.store_check(1, "OK")

    assert incidents.process_check(None, _site(1), _result("OK")) == "closed"

    assert len(store.closed) == 1


def test_a_second_up_result_closes_nothing_more(store):
    """Only the first up result after an incident closes it."""
    store.open_incidents[1] = {"id": 9, "site_id": 1}

    assert incidents.process_check(None, _site(1), _result("OK")) == "closed"
    assert incidents.process_check(None, _site(1), _result("OK")) is None

    assert store.closed == [9]


def test_a_recovery_resets_the_counter_so_a_new_incident_can_open(store):
    """After a recovery a fresh group of failures opens a new incident."""
    for _ in range(3):
        store.store_check(1, "HTTP_5XX")
    assert incidents.process_check(None, _site(1), _result("HTTP_5XX")) == "opened"

    store.store_check(1, "OK")
    assert incidents.process_check(None, _site(1), _result("OK")) == "closed"

    for _ in range(3):
        store.store_check(1, "CONNECT_TIMEOUT")
    assert incidents.process_check(None, _site(1), _result("CONNECT_TIMEOUT")) == "opened"

    assert len(store.opened) == 2
    assert store.opened[1]["failure_type"] == "CONNECT_TIMEOUT"


def test_flapping_opens_and_closes_alternating(store):
    """A site that flaps opens and closes one incident per outage."""
    codes = ["HTTP_5XX", "HTTP_5XX", "HTTP_5XX", "OK",
             "HTTP_5XX", "HTTP_5XX", "HTTP_5XX", "OK"]
    outcomes = []
    for code in codes:
        store.store_check(1, code)
        outcomes.append(incidents.process_check(None, _site(1), _result(code)))

    assert outcomes == [None, None, "opened", "closed", None, None, "opened", "closed"]
    assert len(store.opened) == 2
    assert len(store.closed) == 2


def test_a_short_outage_after_a_recovery_opens_no_incident(store):
    """A recovery resets the counter, so two failures stay below the threshold."""
    for _ in range(3):
        store.store_check(1, "HTTP_5XX")
    incidents.process_check(None, _site(1), _result("HTTP_5XX"))

    store.store_check(1, "OK")
    assert incidents.process_check(None, _site(1), _result("OK")) == "closed"

    store.store_check(1, "HTTP_5XX")
    store.store_check(1, "HTTP_5XX")
    assert incidents.process_check(None, _site(1), _result("HTTP_5XX")) is None

    # Nothing is open any more, so the next up result closes nothing.
    store.store_check(1, "OK")
    assert incidents.process_check(None, _site(1), _result("OK")) is None

    assert len(store.opened) == 1
    assert len(store.closed) == 1


# --------------------------------------------------------------------------
# No second incident while one is open
# --------------------------------------------------------------------------

def test_a_failure_while_an_incident_is_open_opens_no_second_one(store):
    """An extra failure does not open a second incident."""
    store.open_incidents[1] = {"id": 4, "site_id": 1}
    for _ in range(4):
        store.store_check(1, "DNS_FAILURE")

    assert incidents.process_check(None, _site(1), _result("DNS_FAILURE")) is None

    assert store.opened == []
    assert store.closed == []


# --------------------------------------------------------------------------
# Sites are independent of each other
# --------------------------------------------------------------------------

def test_each_site_has_its_own_incident(store):
    """The failure of one site does not open an incident for another one."""
    for _ in range(3):
        store.store_check(1, "HTTP_5XX")
    store.store_check(2, "HTTP_5XX")

    assert incidents.process_check(None, _site(1), _result("HTTP_5XX")) == "opened"
    assert incidents.process_check(None, _site(2), _result("HTTP_5XX")) is None

    assert [incident["site_id"] for incident in store.opened] == [1]


# --------------------------------------------------------------------------
# Error path
# --------------------------------------------------------------------------

def test_a_failing_database_call_does_not_raise(store):
    """A database error inside the state machine is caught and logged."""
    store.errors.add("recent_results")

    assert incidents.process_check(None, _site(1), _result("DNS_FAILURE")) is None


def test_a_failing_open_does_not_raise(store):
    """A database error while opening an incident is caught and logged."""
    for _ in range(3):
        store.store_check(1, "HTTP_5XX")
    store.errors.add("open_incident")

    assert incidents.process_check(None, _site(1), _result("HTTP_5XX")) is None


def test_a_failing_close_does_not_raise(store):
    """A database error while closing an incident is caught and logged."""
    store.open_incidents[1] = {"id": 11, "site_id": 1}
    store.store_check(1, "OK")
    store.errors.add("close_incident")

    assert incidents.process_check(None, _site(1), _result("OK")) is None


def test_a_missing_site_id_does_not_raise(store):
    """A site row without an id is ignored instead of breaking the run."""
    assert incidents.process_check(None, {}, _result("DNS_FAILURE")) is None

    assert store.call_names() == []


def test_a_site_without_a_row_is_ignored(store):
    """A missing site is ignored instead of breaking the run."""
    assert incidents.process_check(None, None, _result("DNS_FAILURE")) is None

    assert store.call_names() == []


def test_a_site_with_an_unusable_id_is_ignored(store):
    """A site whose id is not a number is ignored instead of breaking the run."""
    assert incidents.process_check(None, {"id": "not-a-number"}, _result("DNS_FAILURE")) is None

    assert store.call_names() == []


def test_a_site_object_with_an_id_is_accepted(store):
    """The site may also be an object with an id attribute."""
    site = type("Site", (), {"id": 3})()

    store.store_check(3, "DNS_FAILURE")

    assert incidents.process_check(None, site, _result("DNS_FAILURE")) is None

    assert store.calls_of("recent_results")[0][0] == 3


def test_a_result_may_be_a_plain_string(store):
    """The result may also be the result code as a string."""
    store.store_check(1, "HTTP_5XX")
    store.store_check(1, "HTTP_5XX")
    store.store_check(1, "HTTP_5XX")

    assert incidents.process_check(None, _site(1), "HTTP_5XX") == "opened"


def test_a_result_may_be_a_mapping(store):
    """The result may also be a mapping of column names."""
    store.store_check(1, "HTTP_5XX")
    store.store_check(1, "HTTP_5XX")
    store.store_check(1, "HTTP_5XX")

    assert incidents.process_check(None, _site(1), {"result": "HTTP_5XX"}) == "opened"


def test_a_result_without_a_code_is_not_counted_as_up(store):
    """A result without a classification code does not close an incident."""
    store.open_incidents[1] = {"id": 3, "site_id": 1}

    assert incidents.process_check(None, _site(1), {}) is None

    assert store.closed == []
