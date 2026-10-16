"""Tests for the /health endpoint."""

from app import create_app


def test_health_returns_200_and_json():
    """The /health endpoint should return 200 and JSON without DB or network work."""
    app = create_app()
    client = app.test_client()

    response = client.get("/health")

    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "healthy"