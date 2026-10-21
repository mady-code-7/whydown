"""Flask routes for WhyDown application."""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from functools import wraps

from flask import Flask, current_app, jsonify, request

from .config import CRON_SECRET_MIN_LENGTH, config, load_cron_secret
from .runner import run_all_checks

logger = logging.getLogger(__name__)

# Header a scheduler may use to send the token instead of a query parameter.
CRON_TOKEN_HEADER = "X-Cron-Token"

# The original runner function. It is kept so that a replaced name on this
# module can be told apart from an untouched one.
_ORIGINAL_RUN_ALL_CHECKS = run_all_checks


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


# Placeholder functions to be implemented in later phases
def register_dashboard_routes(app: Flask) -> None:
    """Register dashboard routes (to be implemented in Phase 3)."""


def register_api_routes(app: Flask) -> None:
    """Register API routes (to be implemented in Phase 3)."""


def register_admin_routes(app: Flask) -> None:
    """Register admin routes (to be implemented in Phase 3)."""


def register_checker_routes(app: Flask) -> None:
    """Register checker routes (to be implemented in Phase 2)."""
