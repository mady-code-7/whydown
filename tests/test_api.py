"""Tests for the read-only JSON API (Phase 3B-3).

No real database and no network is used: the database layer is replaced by
fakes, so every test only checks the HTTP behaviour of the routes.

The tests also make sure that no secret (the scheduler token, the database
URL) ever shows up in a response body or in the log.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from fake_db import FakeConnection

from app import create_app, db

# Values that must never appear in a response or in a log.
SECRET_URL = "postgresql://whydown:sup3rs3cret@db.example.invalid/whydown"
SECRET_TOKEN = "0123456789abcdef"

LATEST = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)

SITE_SUMMARIES = [
    {
        "id": 1,
        "name": "Portal",
        "url": "https://portal.test",
        "enabled": True,
        "latest_result": "OK",
        "latest_checked_at": LATEST,
        "checks_24h": 10,
        "up_24h": 9,
        "checks_7d": 100,
        "up_7d": 90,
    },
    {
        "id": 2,
        "name": "Shop",
        "url": "https://shop.test",
        "enabled": False,
        "latest_result": None,
        "latest_checked_at": None,
        "checks_24h": 0,
        "up_24h": 0,
        "checks_7d": 0,
        "up_7d": 0,
    },
]

SITES = [
    {"id": 1, "name": "Portal", "url": "https://portal.test", "enabled": True},
    {"id": 2, "name": "Shop", "url": "https://shop.test", "enabled": False},
]

CHECKS = [
    {
        "id": 11,
        "site_id": 1,
        "checked_at": LATEST,
        "status_code": 200,
        "result": "OK",
        "failed_stage": None,
        "dns_ms": 2,
        "tcp_ms": 3,
        "tls_ms": 4,
        "ttfb_ms": 5,
        "total_ms": 14,
        "resolved_ip": "93.184.216.34",
        "cert_expires_at": None,
        "cert_expiring_soon": False,
        "error_detail": None,
    }
]

INCIDENTS = [
    {
        "id": 5,
        "site_id": 1,
        "started_at": LATEST,
        "ended_at": None,
        "failure_type": "CONNECT_TIMEOUT",
        "consecutive_failures": 3,
    }
]


@pytest.fixture
def connection(monkeypatch):
    """Replace the database connection with a fake one.

    The counter counts how often a connection was opened, so a test can prove
    that a request needs only one.
    """
    conn = FakeConnection()
    opened: list[FakeConnection] = []

    def fake_connect():
        opened.append(conn)
        return conn

    monkeypatch.setattr(db, "_connect", fake_connect)
    conn.opened = opened  # type: ignore[attr-defined]
    return conn


@pytest.fixture
def client(connection, monkeypatch):
    """A test client whose database layer is faked."""
    monkeypatch.setenv("ADMIN_PASSWORD", "a-password-for-the-tests")
    from app import auth

    monkeypatch.setattr(auth, "is_admin", lambda: True)
    application = create_app()
    application.config["TESTING"] = True
    return application.test_client()


@pytest.fixture
def db_calls(monkeypatch):
    """Record the calls the routes make into the database layer."""
    calls: list[dict[str, object]] = []

    def record(name, rows):
        def fake(*args, **kwargs):
            calls.append({"name": name, "args": args, "kwargs": kwargs})
            return list(rows)

        monkeypatch.setattr(db, name, fake)
        return fake

    record("site_summaries", SITE_SUMMARIES)
    record("list_sites", SITES)
    record("get_recent_checks", CHECKS)
    record("list_incidents", INCIDENTS)
    return calls


# --------------------------------------------------------------------------
# GET /api/sites
# --------------------------------------------------------------------------

def test_sites_endpoint_lists_every_site(client, db_calls):
    """The endpoint returns every site with its current state."""
    response = client.get("/api/sites")

    assert response.status_code == 200
    payload = response.get_json()
    assert [site["id"] for site in payload] == [1, 2]
    assert payload[0]["name"] == "Portal"
    assert payload[0]["url"] == "https://portal.test"
    assert payload[0]["enabled"] is True
    assert payload[0]["latest_result"] == "OK"


def test_sites_endpoint_reports_the_uptime_in_percent(client, db_calls):
    """The uptime is a percentage with two decimals."""
    response = client.get("/api/sites")

    payload = response.get_json()
    assert payload[0]["uptime_24h"] == 90.0
    assert payload[0]["uptime_7d"] == 90.0
    assert payload[0]["checks_24h"] == 10


def test_sites_endpoint_rounds_the_uptime_to_two_decimals(client, monkeypatch):
    """A third of the checks up are 33.33 percent."""
    summaries = [dict(SITE_SUMMARIES[0], checks_24h=3, up_24h=1)]
    monkeypatch.setattr(db, "site_summaries", lambda *a, **k: summaries)

    response = client.get("/api/sites")

    assert response.get_json()[0]["uptime_24h"] == 33.33


def test_sites_endpoint_has_no_uptime_without_any_check(client, db_calls):
    """A window without checks has no uptime: null, not 0.0."""
    response = client.get("/api/sites")

    payload = response.get_json()
    assert payload[1]["uptime_24h"] is None
    assert payload[1]["uptime_7d"] is None
    assert payload[1]["checks_24h"] == 0
    assert payload[1]["latest_result"] is None


def test_sites_endpoint_writes_the_latest_check_time_as_utc(client, db_calls):
    """The latest check time is an ISO 8601 UTC string."""
    response = client.get("/api/sites")

    latest = response.get_json()[0]["latest_checked_at"]
    assert latest == "2026-10-21T12:00:00+00:00"
    assert datetime.fromisoformat(latest) == LATEST


def test_sites_endpoint_reads_the_sites_with_one_query(client, db_calls):
    """The site list is read with a single call, not one per site."""
    client.get("/api/sites")

    names = [call["name"] for call in db_calls]
    assert names == ["site_summaries"]


# --------------------------------------------------------------------------
# GET /api/sites/<id>/checks
# --------------------------------------------------------------------------

def test_checks_endpoint_returns_the_check_history(client, db_calls):
    """The endpoint returns the checks of the site."""
    response = client.get("/api/sites/1/checks")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload[0]["id"] == 11
    assert payload[0]["result"] == "OK"
    assert payload[0]["checked_at"] == "2026-10-21T12:00:00+00:00"


def test_checks_endpoint_uses_the_default_limit(client, db_calls):
    """Without a limit parameter the newest 50 checks are read."""
    client.get("/api/sites/1/checks")

    call = next(call for call in db_calls if call["name"] == "get_recent_checks")
    assert call["kwargs"]["limit"] == 50


def test_checks_endpoint_uses_the_given_limit(client, db_calls):
    """A limit parameter is passed on to the database layer."""
    client.get("/api/sites/1/checks?limit=7")

    call = next(call for call in db_calls if call["name"] == "get_recent_checks")
    assert call["kwargs"]["limit"] == 7


def test_checks_endpoint_clamps_a_limit_above_the_maximum(client, db_calls):
    """A limit above the maximum is limited to the maximum."""
    client.get("/api/sites/1/checks?limit=1000")

    call = next(call for call in db_calls if call["name"] == "get_recent_checks")
    assert call["kwargs"]["limit"] == 500


def test_checks_endpoint_rejects_a_limit_that_is_not_a_number(client, db_calls):
    """A limit that is not an integer is a bad request."""
    response = client.get("/api/sites/1/checks?limit=abc")

    assert response.status_code == 400
    assert "error" in response.get_json()
    assert db_calls == []


def test_checks_endpoint_rejects_a_limit_of_zero(client, db_calls):
    """A limit of zero is a bad request."""
    response = client.get("/api/sites/1/checks?limit=0")

    assert response.status_code == 400
    assert db_calls == []


def test_checks_endpoint_rejects_a_negative_limit(client, db_calls):
    """A negative limit is a bad request."""
    response = client.get("/api/sites/1/checks?limit=-5")

    assert response.status_code == 400
    assert db_calls == []


def test_checks_endpoint_rejects_an_empty_limit(client, db_calls):
    """An empty limit is a bad request."""
    response = client.get("/api/sites/1/checks?limit=")

    assert response.status_code == 400
    assert db_calls == []


def test_checks_endpoint_rejects_a_limit_that_int_cannot_read(client, db_calls):
    """A digit that int() cannot read is a bad request, not an error page."""
    response = client.get("/api/sites/1/checks?limit=%C2%B2")

    assert response.status_code == 400
    assert db_calls == []


def test_checks_endpoint_rejects_a_fractional_limit(client, db_calls):
    """A fractional limit is a bad request."""
    response = client.get("/api/sites/1/checks?limit=1.5")

    assert response.status_code == 400
    assert db_calls == []


def test_checks_endpoint_answers_404_for_an_unknown_site(client, db_calls):
    """An unknown site id is answered with a JSON error."""
    response = client.get("/api/sites/99/checks")

    assert response.status_code == 404
    assert "error" in response.get_json()


def test_checks_endpoint_does_not_read_the_checks_of_an_unknown_site(client, db_calls):
    """The history of an unknown site is not read at all."""
    client.get("/api/sites/99/checks")

    assert [call["name"] for call in db_calls] == ["list_sites"]


# --------------------------------------------------------------------------
# GET /api/incidents
# --------------------------------------------------------------------------

def test_incidents_endpoint_lists_the_incidents(client, db_calls):
    """The endpoint returns the incidents with their columns."""
    response = client.get("/api/incidents")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload[0]["id"] == 5
    assert payload[0]["site_id"] == 1
    assert payload[0]["failure_type"] == "CONNECT_TIMEOUT"
    assert payload[0]["consecutive_failures"] == 3
    assert payload[0]["started_at"] == "2026-10-21T12:00:00+00:00"
    assert payload[0]["ended_at"] is None


def test_incidents_endpoint_uses_the_default_limit(client, db_calls):
    """Without a limit parameter the newest 50 incidents are read."""
    client.get("/api/incidents")

    call = next(call for call in db_calls if call["name"] == "list_incidents")
    assert call["kwargs"]["limit"] == 50


def test_incidents_endpoint_uses_the_given_limit(client, db_calls):
    """A limit parameter is passed on to the database layer."""
    client.get("/api/incidents?limit=3")

    call = next(call for call in db_calls if call["name"] == "list_incidents")
    assert call["kwargs"]["limit"] == 3


def test_incidents_endpoint_clamps_a_limit_above_the_maximum(client, db_calls):
    """A limit above the maximum is limited to the maximum."""
    client.get("/api/incidents?limit=9999")

    call = next(call for call in db_calls if call["name"] == "list_incidents")
    assert call["kwargs"]["limit"] == 500


def test_incidents_endpoint_rejects_a_bad_limit(client, db_calls):
    """A limit that is not a positive integer is a bad request."""
    for value in ("abc", "0", "-1", ""):
        response = client.get(f"/api/incidents?limit={value}")

        assert response.status_code == 400
        assert "error" in response.get_json()

    assert db_calls == []


# --------------------------------------------------------------------------
# Method, caching and connection handling
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/sites/1/checks", "/api/incidents"])
def test_api_endpoints_reject_post(client, db_calls, path):
    """The read-only endpoints answer 405 for a POST."""
    response = client.post(path)

    assert response.status_code == 405
    assert "error" in response.get_json()


def test_the_sites_endpoint_is_a_write_route_now(client, db_calls, monkeypatch):
    """POST /api/sites is a write route: it needs a login and a token."""
    from app import auth

    monkeypatch.setattr(auth, "is_admin", lambda: False)
    response = client.post(
        "/api/sites", json={"name": "Portal", "url": "https://portal.test"}
    )

    assert response.status_code == 401
    assert "error" in response.get_json()
    assert db_calls == []


@pytest.mark.parametrize("path", ["/api/sites/1/checks", "/api/incidents"])
def test_a_rejected_post_does_not_open_a_connection(client, db_calls, connection, path):
    """A rejected request never touches the database."""
    client.post(path)

    assert connection.opened == []


@pytest.mark.parametrize("path", ["/api/sites", "/api/sites/1/checks", "/api/incidents"])
def test_api_responses_are_not_cached(client, db_calls, path):
    """Every API response says that it must not be cached."""
    response = client.get(path)

    assert response.headers["Cache-Control"] == "no-store"


def test_an_api_error_is_not_cached(client, db_calls):
    """An error answer is not cached either."""
    response = client.get("/api/sites/1/checks?limit=abc")

    assert response.status_code == 400
    assert response.headers["Cache-Control"] == "no-store"


def test_a_request_uses_one_connection_for_all_its_queries(client, db_calls, connection):
    """A request opens one connection and uses it for every query."""
    client.get("/api/sites/1/checks")

    assert len(connection.opened) == 1


def test_a_request_closes_its_connection(client, db_calls, connection):
    """The connection of a request is closed again."""
    client.get("/api/sites/1/checks")

    assert connection.closed is True


def test_a_failed_request_closes_its_connection(client, monkeypatch, connection):
    """A failing request closes its connection as well."""
    def boom(*args, **kwargs):
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "site_summaries", boom)

    response = client.get("/api/sites")

    assert response.status_code == 503
    assert connection.closed is True


def test_a_rejected_request_does_not_open_a_connection(client, db_calls, connection):
    """A limit that is rejected is answered without touching the database."""
    client.get("/api/sites/1/checks?limit=abc")

    assert connection.opened == []


# --------------------------------------------------------------------------
# Database failures
# --------------------------------------------------------------------------

def test_a_database_failure_answers_503(client, monkeypatch):
    """A failing database is answered with a generic error."""
    def boom(*args, **kwargs):
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "site_summaries", boom)

    response = client.get("/api/sites")

    assert response.status_code == 503
    assert "error" in response.get_json()


def test_a_database_failure_leaks_nothing(client, monkeypatch, caplog):
    """The 503 body and the log hold neither a traceback nor the DSN."""
    def boom(*args, **kwargs):
        raise db.DatabaseError(f"could not connect to {SECRET_URL}")

    monkeypatch.setattr(db, "site_summaries", boom)

    with caplog.at_level(logging.DEBUG):
        response = client.get("/api/sites")

    body = response.get_data(as_text=True)
    assert response.status_code == 503
    assert "Traceback" not in body
    assert SECRET_URL not in body
    assert "sup3rs3cret" not in body
    assert "DATABASE_URL" not in body
    assert SECRET_URL not in caplog.text
    assert "sup3rs3cret" not in caplog.text


def test_a_failing_connection_answers_503(client, monkeypatch):
    """A database that cannot be reached is answered with a 503."""
    def boom():
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "_connect", boom)

    response = client.get("/api/sites")

    assert response.status_code == 503
    assert "error" in response.get_json()


def test_a_failing_check_query_answers_503(client, db_calls, monkeypatch):
    """A failing history query is answered with a 503."""
    def boom(*args, **kwargs):
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "get_recent_checks", boom)

    response = client.get("/api/sites/1/checks")

    assert response.status_code == 503


def test_a_failing_incident_query_answers_503(client, monkeypatch):
    """A failing incident query is answered with a 503."""
    def boom(*args, **kwargs):
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "list_incidents", boom)

    response = client.get("/api/incidents")

    assert response.status_code == 503


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/sites", "/api/sites/1/checks", "/api/incidents"])
def test_no_secret_appears_in_a_response(client, db_calls, monkeypatch, path):
    """Neither the token nor the database URL ends up in a response."""
    monkeypatch.setenv("CRON_SECRET", SECRET_TOKEN)
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)

    response = client.get(path)

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert SECRET_TOKEN not in body
    assert SECRET_URL not in body
    assert "sup3rs3cret" not in body
    assert "CRON_SECRET" not in body
    assert "DATABASE_URL" not in body


# --------------------------------------------------------------------------
# The routes and the database layer together
# --------------------------------------------------------------------------

def test_the_routes_work_with_the_real_database_functions(monkeypatch):
    """The routes and the real database functions fit together.

    Only the connection is faked here, so the SQL, the column names and the
    row format of the database layer are used as they are in production.
    """
    conn = FakeConnection([
        # site_summaries: the sites, then the counts
        [
            {
                "id": 1,
                "name": "Portal",
                "url": "https://portal.test",
                "enabled": True,
                "latest_result": "OK",
                "latest_checked_at": LATEST,
            }
        ],
        [
            {
                "site_id": 1,
                "checks_24h": 4,
                "up_24h": 3,
                "checks_7d": 40,
                "up_7d": 30,
            }
        ],
    ])
    monkeypatch.setattr(db, "_connect", lambda: conn)

    application = create_app()
    application.config["TESTING"] = True
    response = application.test_client().get("/api/sites")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload[0]["name"] == "Portal"
    assert payload[0]["uptime_24h"] == 75.0
    assert payload[0]["uptime_7d"] == 75.0
    assert payload[0]["checks_24h"] == 4
    assert payload[0]["latest_checked_at"] == "2026-10-21T12:00:00+00:00"
    assert conn.closed is True


def test_the_incidents_route_works_with_the_real_database_function(monkeypatch):
    """The incident route reads the rows of the real database function."""
    conn = FakeConnection([
        [
            {
                "id": 5,
                "site_id": 1,
                "started_at": LATEST,
                "ended_at": None,
                "failure_type": "CONNECT_TIMEOUT",
                "consecutive_failures": 3,
            }
        ]
    ])
    monkeypatch.setattr(db, "_connect", lambda: conn)

    application = create_app()
    application.config["TESTING"] = True
    response = application.test_client().get("/api/incidents?limit=10")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload[0]["failure_type"] == "CONNECT_TIMEOUT"
    assert payload[0]["started_at"] == "2026-10-21T12:00:00+00:00"
    # The limit is passed on as a parameter.
    assert conn.statements[0].params == (10,)
    assert conn.closed is True


def test_the_checks_route_works_with_the_real_database_functions(monkeypatch):
    """The check route finds the site and reads its history."""
    conn = FakeConnection([
        # list_sites
        [{"id": 1, "name": "Portal", "url": "https://portal.test", "enabled": True}],
        # get_recent_checks
        [
            {
                "id": 11,
                "site_id": 1,
                "checked_at": LATEST,
                "status_code": 200,
                "result": "OK",
                "failed_stage": None,
                "dns_ms": 2,
                "tcp_ms": 3,
                "tls_ms": 4,
                "ttfb_ms": 5,
                "total_ms": 14,
                "resolved_ip": "93.184.216.34",
                "cert_expires_at": None,
                "cert_expiring_soon": False,
                "error_detail": None,
            }
        ],
    ])
    monkeypatch.setattr(db, "_connect", lambda: conn)

    application = create_app()
    application.config["TESTING"] = True
    response = application.test_client().get("/api/sites/1/checks?limit=5")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload[0]["id"] == 11
    assert payload[0]["result"] == "OK"
    assert payload[0]["checked_at"] == "2026-10-21T12:00:00+00:00"
    # The site id and the limit are passed on as parameters.
    assert conn.statements[1].params == (1, 5)
    # One connection for both queries.
    assert conn.closed is True


# --------------------------------------------------------------------------
# The date and time helper
# --------------------------------------------------------------------------

def test_a_naive_check_time_is_read_as_utc(client, db_calls, monkeypatch):
    """A check time without a time zone is read as UTC."""
    summaries = [dict(SITE_SUMMARIES[0], latest_checked_at=datetime(2026, 10, 21, 12, 0))]
    monkeypatch.setattr(db, "site_summaries", lambda *a, **k: summaries)

    response = client.get("/api/sites")

    assert response.get_json()[0]["latest_checked_at"] == "2026-10-21T12:00:00+00:00"


def test_a_check_time_in_another_time_zone_is_converted_to_utc(client, db_calls, monkeypatch):
    """A check time in another time zone is converted to UTC."""
    from datetime import timedelta, timezone

    other = timezone(timedelta(hours=5))
    summaries = [dict(SITE_SUMMARIES[0], latest_checked_at=datetime(2026, 10, 21, 12, 0, tzinfo=other))]
    monkeypatch.setattr(db, "site_summaries", lambda *a, **k: summaries)

    response = client.get("/api/sites")

    assert response.get_json()[0]["latest_checked_at"] == "2026-10-21T07:00:00+00:00"


def test_a_value_that_is_not_a_date_is_left_alone(client, db_calls, monkeypatch):
    """A value that is not a date is passed through unchanged."""
    summaries = [dict(SITE_SUMMARIES[0], latest_checked_at="not a date")]
    monkeypatch.setattr(db, "site_summaries", lambda *a, **k: summaries)

    response = client.get("/api/sites")

    assert response.get_json()[0]["latest_checked_at"] == "not a date"


def test_a_wrong_method_outside_the_api_keeps_the_default_answer(client):
    """Only the API answers a wrong method with JSON."""
    response = client.post("/health")

    assert response.status_code == 405


def test_an_unknown_site_closes_its_connection(client, db_calls, connection):
    """A request for an unknown site closes its connection as well."""
    client.get("/api/sites/99/checks")

    assert connection.closed is True
    assert len(connection.opened) == 1
