"""Tests for the write routes of the admin area (Phase 5A).

No real database and no network is used: the database layer is replaced by
fakes, so every test only checks what the routes do with the values of a
site.

The authentication itself is checked in ``test_admin_auth.py``; here the
routes are always used with a logged in admin and a CSRF token.
"""

from __future__ import annotations

import re

import pytest
from fake_db import FakeConnection

from app import create_app, db

ADMIN_PASSWORD = "correct-horse-battery-staple"

CSRF_FIELD = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')

# The values of a new site.
NEW_SITE = {
    "name": "Portal",
    "url": "https://portal.test/status",
}


def csrf_token_of(response) -> str:
    """Return the CSRF token of the first form of a page."""
    match = CSRF_FIELD.search(response.get_data(as_text=True))
    assert match is not None, "the page has no CSRF token"
    return match.group(1)


@pytest.fixture(autouse=True)
def clean_rate_limit():
    """Every test starts with an empty rate limit."""
    from app import auth

    auth.reset_login_failures()
    yield
    auth.reset_login_failures()


@pytest.fixture
def fake_db(monkeypatch):
    """Replace every database function the routes use."""
    calls: list[dict[str, object]] = []

    def record(name, result):
        def fake(*args, **kwargs):
            calls.append({"name": name, "args": args, "kwargs": kwargs})
            return result

        monkeypatch.setattr(db, name, fake)

    record("add_site", 42)
    record("update_site", True)
    record("delete_site", True)
    record(
        "get_site",
        {
            "id": 1,
            "name": "Portal",
            "url": "https://portal.test",
            "expected_status": 200,
            "keyword": None,
            "slow_threshold_ms": 5000,
            "enabled": True,
        },
    )
    record("list_sites", [])
    record("site_summaries", [])
    record("get_recent_checks", [])
    record("list_incidents", [])
    monkeypatch.setattr(db, "_connect", lambda: FakeConnection())

    return calls


@pytest.fixture
def client(monkeypatch, fake_db):
    """A test client with an admin password, logged in as an admin."""
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("SECRET_KEY", "a-secret-key-for-the-tests")
    application = create_app({"TESTING": True})
    test_client = application.test_client()

    token = csrf_token_of(test_client.get("/admin/login"))
    test_client.post(
        "/admin/login", data={"password": ADMIN_PASSWORD, "csrf_token": token}
    )

    return test_client


def token_of(client) -> str:
    """Return the CSRF token of the session of the client."""
    return csrf_token_of(client.get("/admin/sites"))


def write_calls(fake_db, name: str) -> list[dict[str, object]]:
    """Return the calls of one write function."""
    return [call for call in fake_db if call["name"] == name]


# --------------------------------------------------------------------------
# POST /api/sites
# --------------------------------------------------------------------------

def test_the_api_stores_a_new_site(client, fake_db):
    """A new site is stored and answered with its id."""
    response = client.post(
        "/api/sites", json=NEW_SITE, headers={"X-CSRF-Token": token_of(client)}
    )

    assert response.status_code == 201
    assert response.get_json()["id"] == 42
    call = write_calls(fake_db, "add_site")[0]
    assert call["kwargs"]["name"] == "Portal"
    assert call["kwargs"]["url"] == "https://portal.test/status"
    assert call["kwargs"]["expected_status"] == 200
    assert call["kwargs"]["slow_threshold_ms"] == 5000
    assert call["kwargs"]["enabled"] is True


def test_the_api_stores_the_values_that_are_given(client, fake_db):
    """Every field of a site can be set."""
    response = client.post(
        "/api/sites",
        json={
            "name": "Shop",
            "url": "https://shop.test",
            "expected_status": 204,
            "keyword": "welcome",
            "slow_threshold_ms": 1500,
            "enabled": False,
        },
        headers={"X-CSRF-Token": token_of(client)},
    )

    assert response.status_code == 201
    call = write_calls(fake_db, "add_site")[0]
    assert call["kwargs"]["expected_status"] == 204
    assert call["kwargs"]["keyword"] == "welcome"
    assert call["kwargs"]["slow_threshold_ms"] == 1500
    assert call["kwargs"]["enabled"] is False


def test_the_api_refuses_a_site_without_a_name(client, fake_db):
    """A site without a name is a bad request."""
    response = client.post(
        "/api/sites",
        json={"url": "https://portal.test"},
        headers={"X-CSRF-Token": token_of(client)},
    )

    assert response.status_code == 400
    assert "error" in response.get_json()
    assert write_calls(fake_db, "add_site") == []


def test_the_api_refuses_a_site_without_a_url(client, fake_db):
    """A site without a URL is a bad request."""
    response = client.post(
        "/api/sites",
        json={"name": "Portal"},
        headers={"X-CSRF-Token": token_of(client)},
    )

    assert response.status_code == 400
    assert write_calls(fake_db, "add_site") == []


@pytest.mark.parametrize(
    "url",
    ["not a url", "ftp://portal.test", "https://", "javascript:alert(1)"],
)
def test_the_api_refuses_a_url_that_is_not_usable(client, fake_db, url):
    """Only a http or https URL with a host name is a site."""
    response = client.post(
        "/api/sites",
        json={"name": "Portal", "url": url},
        headers={"X-CSRF-Token": token_of(client)},
    )

    assert response.status_code == 400
    assert write_calls(fake_db, "add_site") == []


def test_the_api_refuses_an_expected_status_that_is_not_a_number(client, fake_db):
    """The expected status code must be a number."""
    response = client.post(
        "/api/sites",
        json={"name": "Portal", "url": "https://portal.test", "expected_status": "abc"},
        headers={"X-CSRF-Token": token_of(client)},
    )

    assert response.status_code == 400
    assert write_calls(fake_db, "add_site") == []


def test_the_api_refuses_a_slow_threshold_of_zero(client, fake_db):
    """A threshold of zero would report every site as slow."""
    response = client.post(
        "/api/sites",
        json={"name": "Portal", "url": "https://portal.test", "slow_threshold_ms": 0},
        headers={"X-CSRF-Token": token_of(client)},
    )

    assert response.status_code == 400
    assert write_calls(fake_db, "add_site") == []


def test_the_api_refuses_a_body_that_is_not_json(client, fake_db):
    """A body that is not a JSON object is a bad request."""
    response = client.post(
        "/api/sites",
        data="name=Portal",
        headers={"X-CSRF-Token": token_of(client)},
    )

    assert response.status_code == 400
    assert write_calls(fake_db, "add_site") == []


def test_the_api_refuses_more_sites_than_allowed(client, fake_db, monkeypatch):
    """The number of monitored sites is limited (spec section 8.1)."""
    monkeypatch.setattr(
        db, "list_sites", lambda *a, **k: [{"id": i} for i in range(20)]
    )

    response = client.post(
        "/api/sites", json=NEW_SITE, headers={"X-CSRF-Token": token_of(client)}
    )

    assert response.status_code == 400
    assert write_calls(fake_db, "add_site") == []


def test_a_failing_database_answers_503(client, fake_db, monkeypatch):
    """A database that cannot be reached is answered with a 503."""
    def boom(*args, **kwargs):
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "add_site", boom)

    response = client.post(
        "/api/sites", json=NEW_SITE, headers={"X-CSRF-Token": token_of(client)}
    )

    assert response.status_code == 503
    assert "error" in response.get_json()


# --------------------------------------------------------------------------
# PUT /api/sites/<id>
# --------------------------------------------------------------------------

def test_the_api_changes_a_site(client, fake_db):
    """A site is changed with the values that are sent."""
    response = client.put(
        "/api/sites/1",
        json={"name": "New name"},
        headers={"X-CSRF-Token": token_of(client)},
    )

    assert response.status_code == 200
    call = write_calls(fake_db, "update_site")[0]
    assert call["args"][0] == 1
    assert call["kwargs"]["name"] == "New name"
    # The fields that are not sent keep their value.
    assert call["kwargs"]["url"] == "https://portal.test"
    assert call["kwargs"]["enabled"] is True


def test_the_api_answers_404_for_an_unknown_site(client, fake_db, monkeypatch):
    """A site that does not exist cannot be changed."""
    monkeypatch.setattr(db, "get_site", lambda *a, **k: None)

    response = client.put(
        "/api/sites/99",
        json={"name": "New name"},
        headers={"X-CSRF-Token": token_of(client)},
    )

    assert response.status_code == 404
    assert write_calls(fake_db, "update_site") == []


def test_the_api_refuses_a_change_that_is_not_usable(client, fake_db):
    """A change with a bad value is refused and nothing is written."""
    response = client.put(
        "/api/sites/1",
        json={"url": "ftp://portal.test"},
        headers={"X-CSRF-Token": token_of(client)},
    )

    assert response.status_code == 400
    assert write_calls(fake_db, "update_site") == []


# --------------------------------------------------------------------------
# DELETE /api/sites/<id>
# --------------------------------------------------------------------------

def test_the_api_removes_a_site(client, fake_db):
    """A site is removed."""
    response = client.delete(
        "/api/sites/1", headers={"X-CSRF-Token": token_of(client)}
    )

    assert response.status_code == 204
    assert write_calls(fake_db, "delete_site")[0]["args"][0] == 1


def test_the_api_answers_404_when_the_site_is_already_gone(client, fake_db, monkeypatch):
    """A site that is already gone cannot be removed again."""
    monkeypatch.setattr(db, "delete_site", lambda *a, **k: False)

    response = client.delete(
        "/api/sites/1", headers={"X-CSRF-Token": token_of(client)}
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------
# The pages of the admin area
# --------------------------------------------------------------------------

def test_the_admin_page_lists_the_sites(client, fake_db, monkeypatch):
    """The page shows the monitored sites."""
    monkeypatch.setattr(
        db,
        "list_sites",
        lambda *a, **k: [
            {
                "id": 1,
                "name": "Portal",
                "url": "https://portal.test",
                "expected_status": 200,
                "keyword": None,
                "slow_threshold_ms": 5000,
                "enabled": True,
            }
        ],
    )

    page = client.get("/admin/sites").get_data(as_text=True)

    assert "Portal" in page
    assert "https://portal.test" in page


def test_the_admin_page_adds_a_site(client, fake_db):
    """The form of the admin page adds a site."""
    token = token_of(client)

    response = client.post(
        "/admin/sites",
        data={
            "csrf_token": token,
            "name": "Portal",
            "url": "https://portal.test",
            "expected_status": "200",
            "slow_threshold_ms": "5000",
        },
    )

    assert response.status_code == 302
    call = write_calls(fake_db, "add_site")[0]
    assert call["kwargs"]["name"] == "Portal"
    assert call["kwargs"]["enabled"] is True


def test_the_admin_page_refuses_a_bad_url(client, fake_db):
    """A form with a bad URL shows the message and writes nothing."""
    token = token_of(client)

    response = client.post(
        "/admin/sites",
        data={
            "csrf_token": token,
            "name": "Portal",
            "url": "not a url",
        },
    )

    assert response.status_code == 400
    assert "not usable" in response.get_data(as_text=True)
    assert write_calls(fake_db, "add_site") == []


def test_the_admin_page_changes_a_site(client, fake_db):
    """The edit form of the admin page changes a site."""
    token = token_of(client)

    response = client.post(
        "/admin/sites/1/edit",
        data={
            "csrf_token": token,
            "name": "New name",
            "url": "https://portal.test",
            "expected_status": "200",
            "slow_threshold_ms": "5000",
        },
    )

    assert response.status_code == 302
    call = write_calls(fake_db, "update_site")[0]
    assert call["args"][0] == 1
    assert call["kwargs"]["name"] == "New name"


def test_the_admin_page_removes_a_site(client, fake_db):
    """The delete form of the admin page removes a site."""
    token = token_of(client)

    response = client.post(
        "/admin/sites/1/delete", data={"csrf_token": token}
    )

    assert response.status_code == 302
    assert write_calls(fake_db, "delete_site")[0]["args"][0] == 1


def test_the_admin_page_answers_404_for_an_unknown_site(client, fake_db, monkeypatch):
    """A site that does not exist cannot be changed."""
    monkeypatch.setattr(db, "get_site", lambda *a, **k: None)
    token = token_of(client)

    response = client.post(
        "/admin/sites/99/edit",
        data={"csrf_token": token, "name": "New name", "url": "https://portal.test"},
    )

    assert response.status_code == 404
    assert write_calls(fake_db, "update_site") == []
