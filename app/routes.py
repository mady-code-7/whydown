"""Flask routes for WhyDown application."""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from flask import Flask, current_app, jsonify, request

from . import db
from .config import CRON_SECRET_MIN_LENGTH, config, load_cron_secret
from .runner import run_all_checks

logger = logging.getLogger(__name__)

# Header a scheduler may use to send the token instead of a query parameter.
CRON_TOKEN_HEADER = "X-Cron-Token"

# The original runner function. It is kept so that a replaced name on this
# module can be told apart from an untouched one.
_ORIGINAL_RUN_ALL_CHECKS = run_all_checks

# Default and maximum number of rows the read-only API returns.
DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def require_admin_password(func: Callable) -> Callable:
    """Decorator to require admin password for routes."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        # Check for admin password in header or query param
        provided_token = (
            request.headers.get("X-Admin-Password")
            or request.args.get("admin_password")
            or request.form.get("admin_password")
        )

        if not provided_token or provided_token != config.app.admin_password:
            return jsonify({"error": "Admin access required"}), 403

        return func(*args, **kwargs)
    return wrapper


def configured_secret() -> str:
    """Return the configured scheduler secret, or "" when there is none.

    The environment is read on every request so that the secret can be
    rotated without a restart; an app level ``CRON_SECRET`` overrides it.

    A value shorter than ``CRON_SECRET_MIN_LENGTH`` counts as "not
    configured": a weak secret must never open the endpoint. The value is
    never logged and never put into a response (spec section 12).
    """
    candidate = current_app.config.get("CRON_SECRET") or load_cron_secret()
    candidate = candidate.strip()
    if len(candidate) < CRON_SECRET_MIN_LENGTH:
        return ""
    return candidate


def provided_token() -> str:
    """Return the token sent with the request, or "" when there is none.

    The token may arrive as the ``token`` query parameter (cron-job.org uses
    a GET request), in the ``X-Cron-Token`` header or in the body of a POST
    request.
    """
    return (
        request.args.get("token")
        or request.headers.get(CRON_TOKEN_HEADER)
        or request.form.get("token")
        or ""
    )


def require_cron_token(func: Callable) -> Callable:
    """Protect a route with the scheduler token.

    The token is compared with the configured secret using
    ``hmac.compare_digest``. Neither the secret nor the token is ever
    logged or put into a response body (spec section 12).

    * no secret configured -> 503, the endpoint fails closed;
    * no token sent         -> 401;
    * wrong token           -> 403.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        secret = configured_secret()
        if not secret:
            # Fail closed. The body does not name the setting and does not
            # contain its value.
            return jsonify({"error": "Check scheduling is not available."}), 503

        token = provided_token()
        if not token:
            return jsonify({"error": "Token required"}), 401

        if not hmac.compare_digest(token.encode("utf-8"), secret.encode("utf-8")):
            return jsonify({"error": "Invalid token"}), 403

        return func(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------
# Read-only JSON API (spec section 11)
# --------------------------------------------------------------------------

def _iso_utc(value: Any) -> Any:
    """Return a datetime as an ISO 8601 string in UTC.

    A datetime without a time zone is read as UTC. Every other value is
    returned unchanged.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return value


def _parse_limit() -> int | None:
    """Return the limit of the request, or None when it is not usable.

    The default is 50 rows, the maximum is 500. A limit that is not a
    positive integer is rejected (the caller answers 400).
    """
    raw = request.args.get("limit")
    if raw is None:
        return DEFAULT_LIMIT
    raw = raw.strip()
    # isdecimal() and not isdigit(): a digit like "²" is not a number that
    # int() can read, and it must not become an error page.
    if not raw.isdecimal():
        return None
    limit = int(raw)
    if limit <= 0:
        return None
    return min(limit, MAX_LIMIT)


def _uptime(checks: int, up: int) -> float | None:
    """Return the uptime in percent, or None when there was no check.

    A window without a check has no uptime at all, so the value is None and
    not 0.0 - a site that was never checked is not the same as a site that
    was down.
    """
    if not checks:
        return None
    return round(up / checks * 100.0, 2)


@contextmanager
def _api_connection() -> Iterator[Any]:
    """Yield one database connection for a request and close it again.

    The connection is used for every query of the request and is closed in
    the ``finally`` block, also when a query fails.
    """
    conn = db._connect()
    try:
        yield conn
    finally:
        with suppress(Exception):
            conn.close()


def _api_error(message: str, status: int) -> tuple[Any, int]:
    """Return a generic JSON error for the API."""
    return jsonify({"error": message}), status


def _api_sites() -> tuple[Any, int]:
    """Return every site with its current state (spec section 11)."""
    try:
        with _api_connection() as conn:
            rows = db.site_summaries(conn=conn)
    except Exception as exc:  # noqa: BLE001 - any database failure is a 503
        # Only the type of the error is logged: the message of a database
        # error may contain the connection string (spec section 12).
        logger.error("Reading the site list failed (%s)", type(exc).__name__)
        return _api_error("The site list is not available.", 503)

    sites = []
    for row in rows:
        checks_24h = int(row.get("checks_24h") or 0)
        up_24h = int(row.get("up_24h") or 0)
        checks_7d = int(row.get("checks_7d") or 0)
        up_7d = int(row.get("up_7d") or 0)
        sites.append({
            "id": row.get("id"),
            "name": row.get("name"),
            "url": row.get("url"),
            "enabled": bool(row.get("enabled")),
            "latest_result": row.get("latest_result"),
            "latest_checked_at": _iso_utc(row.get("latest_checked_at")),
            "uptime_24h": _uptime(checks_24h, up_24h),
            "uptime_7d": _uptime(checks_7d, up_7d),
            "checks_24h": checks_24h,
        })
    return jsonify(sites), 200


def _api_site_checks(site_id: int) -> tuple[Any, int]:
    """Return the recent checks of one site (spec section 11)."""
    limit = _parse_limit()
    if limit is None:
        return _api_error("The limit must be a positive number.", 400)

    try:
        with _api_connection() as conn:
            sites = db.list_sites(conn=conn)
            if not any(int(site.get("id") or 0) == site_id for site in sites):
                return _api_error("Unknown site.", 404)
            rows = db.get_recent_checks(site_id, limit=limit, conn=conn)
    except Exception as exc:  # noqa: BLE001 - any database failure is a 503
        logger.error("Reading the checks of site %s failed (%s)", site_id, type(exc).__name__)
        return _api_error("The check history is not available.", 503)

    checks = [
        {
            "id": row.get("id"),
            "site_id": row.get("site_id"),
            "checked_at": _iso_utc(row.get("checked_at")),
            "status_code": row.get("status_code"),
            "result": row.get("result"),
            "failed_stage": row.get("failed_stage"),
            "dns_ms": row.get("dns_ms"),
            "tcp_ms": row.get("tcp_ms"),
            "tls_ms": row.get("tls_ms"),
            "ttfb_ms": row.get("ttfb_ms"),
            "total_ms": row.get("total_ms"),
            "resolved_ip": row.get("resolved_ip"),
            "cert_expires_at": _iso_utc(row.get("cert_expires_at")),
            "cert_expiring_soon": bool(row.get("cert_expiring_soon")),
            "error_detail": row.get("error_detail"),
        }
        for row in rows
    ]
    return jsonify(checks), 200


def _api_incidents() -> tuple[Any, int]:
    """Return the incidents, newest first (spec section 11)."""
    limit = _parse_limit()
    if limit is None:
        return _api_error("The limit must be a positive number.", 400)

    try:
        with _api_connection() as conn:
            rows = db.list_incidents(limit=limit, conn=conn)
    except Exception as exc:  # noqa: BLE001 - any database failure is a 503
        logger.error("Reading the incidents failed (%s)", type(exc).__name__)
        return _api_error("The incidents are not available.", 503)

    incidents = [
        {
            "id": row.get("id"),
            "site_id": row.get("site_id"),
            "started_at": _iso_utc(row.get("started_at")),
            "ended_at": _iso_utc(row.get("ended_at")),
            "failure_type": row.get("failure_type"),
            "consecutive_failures": row.get("consecutive_failures"),
        }
        for row in rows
    ]
    return jsonify(incidents), 200


def call_runner() -> dict[str, object]:
    """Run the check engine and return its summary.

    The runner is called through the ``app.runner`` module, so a runner
    that was replaced there is used. A name that was replaced on this module
    wins over it, which keeps both places patchable in a test.
    """
    from . import runner

    func = globals().get("run_all_checks")
    if func is _ORIGINAL_RUN_ALL_CHECKS:
        func = runner.run_all_checks
    return func()


def register(app: Flask) -> None:
    """Register all routes with the Flask application."""

    # werkzeug logs the whole request line, query string included, so the
    # scheduler token would end up in the log. Only warnings and errors are
    # kept from it (spec section 12).
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @app.route("/health")
    def health():
        """Lightweight health check endpoint - no DB or network work."""
        return jsonify({
            "status": "healthy",
            "service": "whydown",
            "version": "0.1.0"
        }), 200

    @app.route("/run-checks", methods=["GET", "POST"])
    @require_cron_token
    def run_checks():
        """Check every enabled site and return the summary of the run.

        The route is called by the external scheduler (spec section 11). The
        checks run synchronously, so the summary of the run is in the
        response when it arrives.
        """
        try:
            summary = call_runner()
            response = jsonify(summary)
        except Exception as exc:  # noqa: BLE001 - any failure must become a 500
            # Only the type of the error is logged: the message of an
            # exception may contain anything and must never leak a secret.
            logger.error("Check run failed (%s)", type(exc).__name__)
            return jsonify({"error": "Check run failed"}), 500

        return response, 200

    # Import and register other route groups here
    # For now, we'll just register the health endpoint
    # Other routes will be implemented in later phases

    register_api_routes(app)


# Placeholder functions to be implemented in later phases
def register_dashboard_routes(app: Flask) -> None:
    """Register dashboard routes (to be implemented in Phase 3)."""


def register_api_routes(app: Flask) -> None:
    """Register the read-only JSON API (spec section 11).

    The API is public and read-only: it never changes a site and never
    starts a check. Every response says that it must not be cached, so a
    client never shows an old state of a site.
    """

    @app.get("/api/sites")
    def api_sites():
        """List the sites with their current state."""
        return _api_sites()

    @app.get("/api/sites/<int:site_id>/checks")
    def api_site_checks(site_id: int):
        """Return the recent checks of one site."""
        return _api_site_checks(site_id)

    @app.get("/api/incidents")
    def api_incidents():
        """Return the incidents, newest first."""
        return _api_incidents()

    @app.after_request
    def _no_store(response):
        """Never let a client cache an API answer."""
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(405)
    def _method_not_allowed(error):
        """Answer a wrong method with a JSON error, not with an HTML page."""
        if request.path.startswith("/api/"):
            return jsonify({"error": "Method not allowed."}), 405
        return error


def register_admin_routes(app: Flask) -> None:
    """Register admin routes (to be implemented in Phase 3)."""


def register_checker_routes(app: Flask) -> None:
    """Register checker routes (to be implemented in Phase 2)."""
