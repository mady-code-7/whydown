"""WhyDown Flask application factory."""

from flask import Flask


def create_app(config: dict | None = None) -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)
    app.config.update(config or {})

    # Register blueprints / routes
    from . import routes

    routes.register(app)

    return app