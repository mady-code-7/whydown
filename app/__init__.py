"""WhyDown Flask application factory."""

from __future__ import annotations

import logging
import os
import secrets
from datetime import timedelta
from typing import Any

from flask import Flask

from . import auth

logger = logging.getLogger(__name__)

# How long an admin session stays open.
SESSION_LIFETIME = timedelta(hours=12)


def _configure_secret(app: Flask) -> None:
    """Set the key the session cookie is signed with.

    The key comes from the ``SECRET_KEY`` environment variable. Without it
    a random key is generated: the application stays usable, but every
    restart ends the admin sessions, because the old cookies cannot be
    read any more.
    """
    secret = app.config.get("SECRET_KEY") or os.getenv("SECRET_KEY", "")
    secret = str(secret).strip()
    if not secret:
        secret = secrets.token_hex(32)
        logger.warning(
            "SECRET_KEY is not set. A random key is used, so the admin "
            "sessions end with every restart."
        )
    app.config["SECRET_KEY"] = secret


def _configure_session(app: Flask) -> None:
    """Protect the cookie of the session.

    The cookie is HttpOnly, so no script can read it, SameSite=Lax, so no
    other site may send it along, and Secure whenever the application does
    not run in debug mode.
    """
    debug = bool(app.config.get("DEBUG")) or os.getenv("FLASK_DEBUG", "").lower() == "true"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = not debug
    app.config["PERMANENT_SESSION_LIFETIME"] = SESSION_LIFETIME


def _template_globals() -> dict[str, Any]:
    """Add the helpers every template needs.

    ``csrf_token`` is the function that hands out the token of the
    session, ``is_admin`` says whether the visitor may use the admin
    area.
    """
    return {"csrf_token": auth.csrf_token, "is_admin": auth.is_admin}


def create_app(config: dict | None = None) -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)
    app.config.update(config or {})

    _configure_secret(app)
    _configure_session(app)

    # Register blueprints / routes
    from . import routes

    routes.register(app)

    app.context_processor(_template_globals)

    return app
