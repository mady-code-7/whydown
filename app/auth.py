"""Admin authentication for the write routes of WhyDown (spec section 12).

Design rules:

* The admin password comes from the ``ADMIN_PASSWORD`` environment variable
  only. It is never hardcoded, never logged and never part of a response or
  of an error message.
* A missing or an empty password closes every write route: the application
  fails closed instead of letting everybody in.
* The password is compared with ``hmac.compare_digest``, so the comparison
  takes the same time for every input.
* The login is kept in a signed session cookie. The cookie is HttpOnly
  (no script can read it), SameSite=Lax (no other site may send it along)
  and Secure whenever the application does not run in debug mode.
* Every write request needs a CSRF token that belongs to the session.
* A failed login is logged without the password, and a burst of failed
  logins locks an address out for a while.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import (
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

logger = logging.getLogger(__name__)

# The session entry that says that an admin is logged in.
SESSION_LOGIN_KEY = "admin_logged_in"

# The session entry that holds the CSRF token.
SESSION_CSRF_KEY = "csrf_token"

# The header a JSON client may use instead of a form field.
CSRF_HEADER = "X-CSRF-Token"

# The name of the field that holds the CSRF token.
CSRF_FIELD = "csrf_token"

# The name of the field that holds the password.
PASSWORD_FIELD = "password"

# The number of failed logins that lock an address out, and the length of
# the window the failures are counted in. The lock-out lasts one window
# after the last failure.
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 600

# The failed logins per address and the time the lock-out ends.
_login_failures: dict[str, list[float]] = {}
_login_locked_until: dict[str, float] = {}
_login_lock = threading.Lock()


def _now() -> float:
    """Return the current time in seconds.

    It is a function of its own, so a test can hold the clock still while
    it looks at the rate limit.
    """
    return time.monotonic()


# --------------------------------------------------------------------------
# The password
# --------------------------------------------------------------------------

def configured_admin_password() -> str:
    """Return the admin password of the application, or "" when there is none.

    The password is read from the environment on every call, so it can be
    rotated without a restart. An app level ``ADMIN_PASSWORD`` (set in
    ``create_app``) wins over the environment.

    The value is never logged and never put into a response (spec
    section 12).
    """
    override = current_app.config.get("ADMIN_PASSWORD")
    if override is None:
        override = os.getenv("ADMIN_PASSWORD", "")
    return str(override).strip()


def admin_configured() -> bool:
    """Return True when a password is configured.

    Without a password nobody can log in, so every write route stays
    closed: the application fails closed.
    """
    return bool(configured_admin_password())


def password_matches(given: str) -> bool:
    """Return True when ``given`` is the configured password.

    The comparison uses ``hmac.compare_digest``, so it does not leak how
    much of the password was right.
    """
    expected = configured_admin_password()
    if not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


# --------------------------------------------------------------------------
# The session
# --------------------------------------------------------------------------

def is_admin() -> bool:
    """Return True when the current session belongs to a logged in admin."""
    return bool(session.get(SESSION_LOGIN_KEY))


def start_admin_session() -> None:
    """Open the session of an admin.

    The session is renewed first: everything that was in it before the
    login is thrown away, so a token of an earlier session can never be
    used after the login.
    """
    session.clear()
    session[SESSION_LOGIN_KEY] = True
    session.permanent = True
    # A new CSRF token belongs to the new session.
    session.pop(SESSION_CSRF_KEY, None)


def end_admin_session() -> None:
    """Close the session of an admin."""
    session.clear()


# --------------------------------------------------------------------------
# The CSRF token
# --------------------------------------------------------------------------

def csrf_token() -> str:
    """Return the CSRF token of the session, creating it when needed.

    The token is kept in the signed session cookie. A form shows it in a
    hidden field, a JSON client sends it in the ``X-CSRF-Token`` header.
    """
    token = session.get(SESSION_CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[SESSION_CSRF_KEY] = token
    return token


def provided_csrf_token() -> str:
    """Return the CSRF token of the request, or "" when there is none.

    The token may arrive in the ``X-CSRF-Token`` header, in the JSON body
    or in the body of a form.
    """
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header.strip()

    if request.is_json:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            value = data.get(CSRF_FIELD)
            if isinstance(value, str):
                return value.strip()

    return request.form.get(CSRF_FIELD, "").strip()


def csrf_valid() -> bool:
    """Return True when the request carries the CSRF token of its session.

    A request without a token and a request with a token that does not
    belong to the session are both refused.
    """
    expected = session.get(SESSION_CSRF_KEY)
    if not expected:
        return False
    given = provided_csrf_token()
    if not given:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


# --------------------------------------------------------------------------
# The rate limit of the login
# --------------------------------------------------------------------------

def _prune_failures(address: str, now: float) -> list[float]:
    """Drop the failures that are older than the window."""
    times = [t for t in _login_failures.get(address, []) if now - t < LOGIN_WINDOW_SECONDS]
    if times:
        _login_failures[address] = times
    else:
        _login_failures.pop(address, None)
    return times


def login_locked(address: str, now: float | None = None) -> bool:
    """Return True when the address may not try to log in right now.

    The address is locked out when it failed ``MAX_LOGIN_ATTEMPTS`` times
    inside the window. The lock-out then lasts one window.
    """
    moment = _now() if now is None else now
    with _login_lock:
        until = _login_locked_until.get(address)
        if until is not None:
            if moment < until:
                return True
            del _login_locked_until[address]

        times = _prune_failures(address, moment)
        if len(times) >= MAX_LOGIN_ATTEMPTS:
            _login_locked_until[address] = times[-1] + LOGIN_WINDOW_SECONDS
            return True
        return False


def record_login_failure(address: str, now: float | None = None) -> None:
    """Write down a failed login of an address."""
    moment = _now() if now is None else now
    with _login_lock:
        times = _prune_failures(address, moment)
        times.append(moment)
        _login_failures[address] = times


def clear_login_failures(address: str) -> None:
    """Forget the failed logins of an address."""
    with _login_lock:
        _login_failures.pop(address, None)
        _login_locked_until.pop(address, None)


def reset_login_failures() -> None:
    """Forget every failed login.

    It is used by the tests, so one test never sees the failures of
    another one.
    """
    with _login_lock:
        _login_failures.clear()
        _login_locked_until.clear()


def client_address() -> str:
    """Return the address of the client, for the rate limit and the log."""
    return request.remote_addr or "unknown"


# --------------------------------------------------------------------------
# The decorators of the write routes
# --------------------------------------------------------------------------

def _is_api_request() -> bool:
    """Return True when the request goes to the JSON API."""
    return request.path.startswith("/api/")


def _not_configured() -> Any:
    """Answer a request that can never be authenticated."""
    if _is_api_request():
        return jsonify({"error": "Admin access is not available."}), 503
    return redirect(url_for("admin_login"))


def admin_required(func: Callable) -> Callable:
    """Protect a route with the admin login.

    A request without a session is answered with a JSON error and a 401
    for the API, and with a redirect to the login page for a page.

    When no password is configured the route fails closed: the answer is
    a 503 for the API and a redirect to the login page, which explains
    that the admin access is not set up.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not admin_configured():
            return _not_configured()
        if not is_admin():
            if _is_api_request():
                return jsonify({"error": "Admin login required."}), 401
            return redirect(url_for("admin_login", next=request.path))
        return func(*args, **kwargs)

    return wrapper


def csrf_protect(func: Callable) -> Callable:
    """Protect a route with the CSRF token of the session.

    A request without a token and a request with a token that does not
    belong to the session are both answered with a 403.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not csrf_valid():
            if _is_api_request():
                return jsonify({"error": "Invalid CSRF token."}), 403
            return render_template("forbidden.html"), 403
        return func(*args, **kwargs)

    return wrapper
