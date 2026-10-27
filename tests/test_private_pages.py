"""Tests that the dashboard and the read API are private (phase 8, part 3B)."""

import pytest

from app import create_app, db

PAGES = ["/", "/sites/1", "/incidents"]
API_PATHS = ["/api/sites", "/api/sites/1/checks", "/api/incidents"]


@pytest.fixture
def opened():
    """Record every database connection a request opens."""
    return []


@pytest.fixture
def client(monkeypatch, opened):
    """A test client that is not logged in, on an app with an admin password."""
    monkeypatch.setenv("ADMIN_PASSWORD", "a-password-for-the-tests")
    monkeypatch.setenv("SECRET_KEY", "a-secret-key-for-the-tests")

    def fake_connect():
        opened.append(True)
        raise RuntimeError("the database must not be used")

    monkeypatch.setattr(db, "_connect", fake_connect)
    return create_app({"TESTING": True}).test_client()


@pytest.mark.parametrize("path", PAGES)
def test_a_visitor_is_sent_to_the_login_page_from_every_page(client, path):
    response = client.get(path)

    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]


@pytest.mark.parametrize("path", API_PATHS)
def test_a_visitor_gets_a_401_json_error_from_every_read_api(client, path):
    response = client.get(path)

    assert response.status_code == 401
    assert response.get_json() == {"error": "Admin login required."}


@pytest.mark.parametrize("path", PAGES + API_PATHS)
def test_a_rejected_visit_never_opens_a_database_connection(client, opened, path):
    client.get(path)

    assert opened == []


def test_the_login_page_stays_open_to_visitors(client):
    assert client.get("/admin/login").status_code == 200


def test_the_health_route_stays_open_to_visitors(client):
    assert client.get("/health").status_code == 200


def test_the_public_check_preflight_stays_open_to_visitors(client):
    response = client.options(
        "/api/public/check",
        headers={"Origin": "https://whydown-nm.web.app", "Access-Control-Request-Method": "POST"},
    )

    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == "*"


def test_the_public_check_route_is_not_locked_behind_the_login(client):
    response = client.post("/api/public/check", json={})

    assert response.status_code == 400
    assert response.get_json() == {"error": "Send a JSON body with a url."}


def test_the_scheduler_route_is_not_locked_behind_the_admin_login(client):
    response = client.get("/run-checks")

    assert response.status_code != 302
    assert (response.get_json(silent=True) or {}).get("error") != "Admin login required."


def test_the_pages_fail_closed_when_no_admin_password_is_set(client, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    page = client.get("/")
    api = client.get("/api/sites")

    assert page.status_code == 302
    assert "/admin/login" in page.headers["Location"]
    assert api.status_code == 503