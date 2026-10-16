"""Flask routes for WhyDown application."""

from __future__ import annotations

from flask import Flask, jsonify, request, render_template
from functools import wraps
import logging
from typing import Callable

logger = logging.getLogger(__name__)


def require_admin_password(func: Callable) -> Callable:
    """Decorator to require admin password for routes."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        from . import config

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


def require_cron_token(func: Callable) -> Callable:
    """Decorator to require cron token for /run-checks."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        from . import config

        # Check for token in query param (cron-job.org uses GET)
        provided_token = request.args.get("token")

        if not provided_token or provided_token != config.app.secret_key:
            return jsonify({"error": "Invalid token"}), 401

        return func(*args, **kwargs)
    return wrapper


def register(app: Flask) -> None:
    """Register all routes with the Flask application."""

    @app.route("/health")
    def health():
        """Lightweight health check endpoint - no DB or network work."""
        return jsonify({
            "status": "healthy",
            "service": "whydown",
            "version": "0.1.0"
        }), 200

    # Import and register other route groups here
    # For now, we'll just register the health endpoint
    # Other routes will be implemented in later phases


# Placeholder functions to be implemented in later phases
def register_dashboard_routes(app: Flask) -> None:
    """Register dashboard routes (to be implemented in Phase 3)."""
    pass


def register_api_routes(app: Flask) -> None:
    """Register API routes (to be implemented in Phase 3)."""
    pass


def register_admin_routes(app: Flask) -> None:
    """Register admin routes (to be implemented in Phase 3)."""
    pass


def register_checker_routes(app: Flask) -> None:
    """Register checker routes (to be implemented in Phase 2)."""
    pass