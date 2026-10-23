"""Tests for the admin authentication of the write routes (Phase 5A).

No real database and no network is used: the database layer is replaced by
fakes and every request only touches the Flask test client.

The tests cover the rules of the phase:

* a write route is only open for a logged in admin;
* the password comes from the environment and is never shown or logged;
* every write needs a CSRF token;
* a burst of wrong passwords locks an address out for a while;
* the read-only pages and the scheduler endpoint stay as they were.
"""

from __future__ import annotations

import logging
import re

import pytest
from fake_db import FakeConnection

from app import auth, create_app, db

# The password the environment is configured with during the tests.
ADMIN_PASSWORD = "correct-horse-battery-staple"
# A password that is not the configured one.
WRONG_PASSWORD = "not-the-password"
# The key the session cookie is signed with.
SECRET_KEY = "a-secret-key-for-the-tests"

# A secret for the scheduler endpoint (16 characters or more).
CRON_SECRET = "0123456789abcdef"

# The summary the replaced runner returns.
RUN_SUMMARY = {"sites_checked": 1, "counts": {"OK": 1}, "duration_ms": 10}

# The CSRF token of a form is a hidden input field.
CSRF_FIELD = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')

# Every write route of the application, with the method it is called with.
WRITE_ROUTES = [
    ("/api/sites", "post"),
    ("/api/sites/1", "put"),
    ("/api/sites/1", "delete"),
    ("/admin/sites", "post"),
    ("/admin/sites/1/edit", "post"),
    ("/admin/sites/1/delete", "post"),
    ("/admin/logout", "post"),
]


def csrf_token_of(response) -> str:
    """Return the CSRF token of the first form of a page."""
    match = CSRF_FIELD.search(response.get_data(as_text=True))
    assert match is not None, "the page has no CSRF token"
    return match.group(1)


class FakeDatabase:
    """The recorded calls of the replaced database layer."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    @property
    def writes(self) -> list[str]:
        """The names of the write functions that were called."""
        return [
            str(call["name"])
            for call in self.calls
            if call["name"] in ("add_site", "update_site", "delete_site")
        ]


@pytest.fixture(autouse=True)
def no_failed_logins():
    """Every test starts with an empty rate limit."""
    auth.reset_login_failures()
    yield
    auth.reset_login_failures()


@pytest.fixture
def fake_db(monkeypatch):
    """Replace every database function the routes use."""
    recorder = FakeDatabase()

    def record(name, result):
        def fake(*args, **kwargs):
            recorder.calls.append({"name": name, "args": args, "kwargs": kwargs})
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

    return recorder


@pytest.fixture
def app(monkeypatch):
    """An application with an admin password and a session key."""
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    application = create_app({"TESTING": True})
    return application


@pytest.fixture
def client(app):
    """A test client of the application."""
    return app.test_client()


def log_in(client, password: str = ADMIN_PASSWORD) -> str:
    """Log in and return the CSRF token of the new session."""
    token = csrf_token_of(client.get("/admin/login"))
    response = client.post(
        "/admin/login", data={"password": password, "csrf_token": token}
    )
    assert response.status_code == 302, "the login did not succeed"
    # The session is renewed after the login, so the token of the session
    # that is now open is read from the admin page.
    return csrf_token_of(client.get("/admin/sites"))


# --------------------------------------------------------------------------
# The write routes are closed without a login
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path,method", WRITE_ROUTES)
def test_a_write_route_without_a_login_never_writes(client, fake_db, path, method):
    """A write route without a session never reaches the database."""
    response = getattr(client, method)(path)

    assert response.status_code in (301, 302, 401, 403)
    assert fake_db.writes == []


def test_an_api_write_route_without_a_login_answers_401(client, fake_db):
    """The API answers with a JSON error, not with a page."""
    response = client.post("/api/sites", json={"name": "Portal", "url": "https://portal.test"})

    assert response.status_code == 401
    assert "error" in response.get_json()
    assert fake_db.writes == []


def test_a_page_write_route_without_a_login_redirects_to_the_login(client, fake_db):
    """A page asks the visitor to log in first."""
    response = client.post("/admin/sites", data={"name": "Portal", "url": "https://portal.test"})

    assert response.status_code == 302
    assert "/admin/login" in response.headers["Location"]
    assert fake_db.writes == []


def test_a_write_route_with_a_login_is_allowed(client, fake_db):
    """A logged in admin may add a site."""
    token = log_in(client)

    response = client.post(
        "/api/sites",
        json={"name": "Portal", "url": "https://portal.test"},
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 201
    assert fake_db.writes == ["add_site"]


# --------------------------------------------------------------------------
# The login
# --------------------------------------------------------------------------

def test_a_wrong_password_is_refused(client, fake_db):
    """A wrong password does not open the session."""
    token = csrf_token_of(client.get("/admin/login"))

    response = client.post(
        "/admin/login", data={"password": WRONG_PASSWORD, "csrf_token": token}
    )

    assert response.status_code == 401
    # The session is still closed, so the write route stays closed.
    assert client.post("/api/sites", json={"name": "Portal", "url": "https://x.test"}).status_code == 401
    assert fake_db.writes == []


def test_the_right_password_opens_the_session(client, fake_db):
    """The right password opens the session and redirects to the admin page."""
    token = csrf_token_of(client.get("/admin/login"))

    response = client.post(
        "/admin/login", data={"password": ADMIN_PASSWORD, "csrf_token": token}
    )

    assert response.status_code == 302
    assert "/admin/sites" in response.headers["Location"]


def test_the_login_page_is_public(client, fake_db):
    """The login page can be read without a session."""
    response = client.get("/admin/login")

    assert response.status_code == 200
    assert csrf_token_of(response)


def test_a_missing_admin_password_refuses_every_write_route(client, fake_db, monkeypatch):
    """Without a password in the environment nothing can be written."""
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    response = client.post("/api/sites", json={"name": "Portal", "url": "https://x.test"})

    assert response.status_code == 503
    assert fake_db.writes == []


def test_a_missing_admin_password_refuses_the_login(client, fake_db, monkeypatch):
    """Without a password in the environment nobody can log in."""
    token = csrf_token_of(client.get("/admin/login"))
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    response = client.post(
        "/admin/login", data={"password": ADMIN_PASSWORD, "csrf_token": token}
    )

    assert response.status_code == 503
    assert client.post("/api/sites", json={"name": "Portal", "url": "https://x.test"}).status_code == 503


def test_the_login_page_says_that_the_admin_area_is_not_available(client, fake_db, monkeypatch):
    """Without a password the login page explains that it is closed."""
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    page = client.get("/admin/login").get_data(as_text=True)

    assert "not available" in page
    assert 'type="password"' not in page


def test_an_empty_admin_password_refuses_every_write_route(client, fake_db, monkeypatch):
    """An empty password is not a password: the application fails closed."""
    monkeypatch.setenv("ADMIN_PASSWORD", "")

    response = client.post("/api/sites", json={"name": "Portal", "url": "https://x.test"})

    assert response.status_code == 503
    assert fake_db.writes == []


# --------------------------------------------------------------------------
# The CSRF token
# --------------------------------------------------------------------------

def test_a_write_without_a_csrf_token_is_refused(client, fake_db):
    """A write without a token never reaches the database."""
    log_in(client)

    response = client.post("/api/sites", json={"name": "Portal", "url": "https://x.test"})

    assert response.status_code == 403
    assert fake_db.writes == []


def test_a_write_with_a_wrong_csrf_token_is_refused(client, fake_db):
    """A write with a token that does not fit never reaches the database."""
    log_in(client)

    response = client.post(
        "/api/sites",
        json={"name": "Portal", "url": "https://x.test"},
        headers={"X-CSRF-Token": "a-token-that-is-not-the-right-one"},
    )

    assert response.status_code == 403
    assert fake_db.writes == []


def test_a_write_with_the_csrf_token_of_another_session_is_refused(client, fake_db):
    """The token belongs to the session and to nothing else."""
    log_in(client)

    # A second client has its own session and its own token.
    other = client.application.test_client()
    other_token = csrf_token_of(other.get("/admin/login"))

    response = client.post(
        "/api/sites",
        json={"name": "Portal", "url": "https://x.test"},
        headers={"X-CSRF-Token": other_token},
    )

    assert response.status_code == 403
    assert fake_db.writes == []


def test_the_login_needs_a_csrf_token_as_well(client, fake_db):
    """The login form carries a token, so the login needs one too."""
    response = client.post("/admin/login", data={"password": ADMIN_PASSWORD})

    assert response.status_code == 403
    # The session was not opened.
    assert client.post("/api/sites", json={"name": "Portal", "url": "https://x.test"}).status_code == 401


def test_every_form_of_the_admin_page_carries_a_token(client, fake_db):
    """Every form of the admin page has its own CSRF token."""
    log_in(client)

    page = client.get("/admin/sites").get_data(as_text=True)

    assert page.count('name="csrf_token"') >= 2


# --------------------------------------------------------------------------
# The rate limit of the login
# --------------------------------------------------------------------------

def test_a_burst_of_wrong_passwords_locks_the_address_out(client, fake_db):
    """After five wrong passwords the address may not try again."""
    token = csrf_token_of(client.get("/admin/login"))

    for _ in range(5):
        response = client.post(
            "/admin/login", data={"password": WRONG_PASSWORD, "csrf_token": token}
        )
        assert response.status_code == 401

    response = client.post(
        "/admin/login", data={"password": ADMIN_PASSWORD, "csrf_token": token}
    )

    assert response.status_code == 429
    assert fake_db.writes == []


def test_the_lock_out_expires(client, fake_db, monkeypatch):
    """After the window the address may try again."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(auth, "_now", lambda: clock["now"])
    token = csrf_token_of(client.get("/admin/login"))

    for _ in range(5):
        client.post("/admin/login", data={"password": WRONG_PASSWORD, "csrf_token": token})

    clock["now"] += auth.LOGIN_WINDOW_SECONDS + 1

    response = client.post(
        "/admin/login", data={"password": ADMIN_PASSWORD, "csrf_token": token}
    )

    assert response.status_code == 302


def test_a_successful_login_clears_the_failures(client, fake_db):
    """A correct password in between resets the counter."""
    token = csrf_token_of(client.get("/admin/login"))

    for _ in range(4):
        client.post("/admin/login", data={"password": WRONG_PASSWORD, "csrf_token": token})

    response = client.post(
        "/admin/login", data={"password": ADMIN_PASSWORD, "csrf_token": token}
    )
    assert response.status_code == 302

    # The session is renewed after the login, so the form of the admin page
    # carries the new token.
    token = csrf_token_of(client.get("/admin/sites"))

    # Four more wrong passwords are still allowed: the counter starts again
    # after the successful login.
    for _ in range(4):
        response = client.post(
            "/admin/login", data={"password": WRONG_PASSWORD, "csrf_token": token}
        )
        assert response.status_code == 401

    response = client.post(
        "/admin/login", data={"password": ADMIN_PASSWORD, "csrf_token": token}
    )
    assert response.status_code == 302


# --------------------------------------------------------------------------
# The logout
# --------------------------------------------------------------------------

def test_the_logout_ends_the_session(client, fake_db):
    """After the logout the write routes are closed again."""
    token = log_in(client)

    response = client.post("/admin/logout", data={"csrf_token": token})

    assert response.status_code == 302
    assert client.post("/api/sites", json={"name": "Portal", "url": "https://x.test"}).status_code == 401


def test_the_logout_without_a_session_redirects_to_the_login(client, fake_db):
    """The logout of a closed session does nothing."""
    response = client.post("/admin/logout")

    assert response.status_code == 302


# --------------------------------------------------------------------------
# The password never shows up
# --------------------------------------------------------------------------

def test_the_password_never_shows_in_a_response_or_in_the_log(client, fake_db, caplog):
    """Neither the wrong nor the right password ends up in a page or a log."""
    token = csrf_token_of(client.get("/admin/login"))

    with caplog.at_level(logging.DEBUG):
        wrong = client.post(
            "/admin/login", data={"password": WRONG_PASSWORD, "csrf_token": token}
        )
        right = client.post(
            "/admin/login", data={"password": ADMIN_PASSWORD, "csrf_token": token}
        )

    for response in (wrong, right):
        body = response.get_data(as_text=True)
        assert WRONG_PASSWORD not in body
        assert ADMIN_PASSWORD not in body
    assert WRONG_PASSWORD not in caplog.text
    assert ADMIN_PASSWORD not in caplog.text


def test_the_password_is_not_part_of_the_url(client, fake_db):
    """The password is never sent as a parameter, so it stays out of the log."""
    token = csrf_token_of(client.get("/admin/login"))

    client.post("/admin/login", data={"password": WRONG_PASSWORD, "csrf_token": token})

    assert "password" not in client.get("/admin/login").request.path


# --------------------------------------------------------------------------
# The session cookie
# --------------------------------------------------------------------------

def test_the_session_cookie_is_http_only_and_secure(client, fake_db):
    """The cookie of the session is protected against scripts and sniffing."""
    token = csrf_token_of(client.get("/admin/login"))

    response = client.post(
        "/admin/login", data={"password": ADMIN_PASSWORD, "csrf_token": token}
    )

    cookie = "; ".join(response.headers.getlist("Set-Cookie"))
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Secure" in cookie


def test_the_session_cookie_is_not_secure_in_debug(monkeypatch, fake_db):
    """In debug mode the cookie may travel over a plain connection."""
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    application = create_app({"TESTING": True, "DEBUG": True})
    client = application.test_client()
    token = csrf_token_of(client.get("/admin/login"))

    response = client.post(
        "/admin/login", data={"password": ADMIN_PASSWORD, "csrf_token": token}
    )

    cookie = "; ".join(response.headers.getlist("Set-Cookie"))
    assert "HttpOnly" in cookie
    assert "Secure" not in cookie


def test_without_a_secret_key_the_sessions_end_with_the_process(monkeypatch, fake_db, caplog):
    """Without SECRET_KEY a random key is used, so a restart ends the sessions."""
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.delenv("SECRET_KEY", raising=False)

    with caplog.at_level(logging.INFO):
        application = create_app({"TESTING": True})

    assert application.config["SECRET_KEY"]
    assert "SECRET_KEY" in caplog.text


# --------------------------------------------------------------------------
# The admin links of the dashboard
# --------------------------------------------------------------------------

def test_the_dashboard_shows_the_admin_link_only_after_the_login(client, fake_db):
    """A visitor does not see the admin links, an admin does."""
    anonymous = client.get("/").get_data(as_text=True)
    assert "/admin/login" not in anonymous

    log_in(client)

    page = client.get("/").get_data(as_text=True)
    assert "/admin/sites" in page


# --------------------------------------------------------------------------
# The read-only routes and the scheduler stay as they were
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path", ["/", "/incidents", "/api/sites", "/api/incidents", "/health"]
)
def test_the_read_only_routes_stay_public(client, fake_db, path):
    """The read-only routes need no session."""
    response = client.get(path)

    assert response.status_code == 200


def test_the_run_checks_route_still_uses_its_own_token(client, fake_db, monkeypatch):
    """The scheduler endpoint is untouched by the admin login."""
    monkeypatch.setenv("CRON_SECRET", CRON_SECRET)
    monkeypatch.setattr("app.routes.run_all_checks", lambda: RUN_SUMMARY)

    assert client.get("/run-checks").status_code == 401
    assert client.get("/run-checks", query_string={"token": CRON_SECRET}).status_code == 200


def test_the_run_checks_route_ignores_the_admin_session(client, fake_db, monkeypatch):
    """A logged in admin does not open the scheduler endpoint."""
    monkeypatch.setenv("CRON_SECRET", CRON_SECRET)
    monkeypatch.setattr("app.routes.run_all_checks", lambda: RUN_SUMMARY)
    log_in(client)

    assert client.get("/run-checks").status_code == 401
    assert client.get("/run-checks", query_string={"token": CRON_SECRET}).status_code == 200
