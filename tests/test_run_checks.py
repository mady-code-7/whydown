"""Tests for the /run-checks endpoint (Phase 3B-2).

The check runner is always replaced, so no test here touches the database or
the network. The tests also make sure that neither the secret nor the token
ever shows up in a response body or in the log.
"""

from __future__ import annotations

import logging

import pytest

from app import create_app

# A secret that is long enough to be accepted (16 characters or more).
VALID_SECRET = "0123456789abcdef"
# Too short: a weak secret must not open the endpoint.
TOO_SHORT_SECRET = "short"
# A token that does not match the secret.
WRONG_TOKEN = "ffffffffffffffff"

# The summary the replaced runner returns.
SUMMARY = {
    "sites_checked": 2,
    "counts": {"OK": 1, "HTTP_5XX": 1},
    "duration_ms": 123,
}


@pytest.fixture
def client(monkeypatch):
    """A test client with a valid secret configured."""
    monkeypatch.setenv("CRON_SECRET", VALID_SECRET)
    application = create_app()
    application.config["TESTING"] = True
    return application.test_client()


@pytest.fixture
def runner(monkeypatch):
    """Replace the runner with one that returns the summary."""
    monkeypatch.setattr("app.routes.run_all_checks", lambda: SUMMARY)


def test_missing_token_returns_401(client):
    """Without a token the endpoint answers 401."""
    response = client.get("/run-checks")

    assert response.status_code == 401
    assert "error" in response.get_json()


def test_wrong_token_returns_403(client):
    """A token that does not match the secret answers 403."""
    response = client.get("/run-checks", query_string={"token": WRONG_TOKEN})

    assert response.status_code == 403
    assert "error" in response.get_json()


def test_unset_secret_returns_503(monkeypatch):
    """Without a configured secret the endpoint fails closed with 503."""
    monkeypatch.delenv("CRON_SECRET", raising=False)
    application = create_app()
    application.config["TESTING"] = True
    client = application.test_client()

    response = client.get("/run-checks")
    assert response.status_code == 503

    # A token is sent as well, but there is nothing to compare it with.
    response = client.get("/run-checks", query_string={"token": VALID_SECRET})
    assert response.status_code == 503


def test_too_short_secret_returns_503(monkeypatch):
    """A secret below the minimum length counts as not configured."""
    monkeypatch.setenv("CRON_SECRET", TOO_SHORT_SECRET)
    application = create_app()
    application.config["TESTING"] = True
    client = application.test_client()

    response = client.get("/run-checks", query_string={"token": TOO_SHORT_SECRET})

    assert response.status_code == 503


def test_503_body_reveals_neither_secret_nor_setting(monkeypatch):
    """The 503 body must not name the setting or contain its value."""
    monkeypatch.delenv("CRON_SECRET", raising=False)
    application = create_app()
    client = application.test_client()

    response = client.get("/run-checks", query_string={"token": VALID_SECRET})

    body = response.get_data(as_text=True)
    assert response.status_code == 503
    assert "CRON_SECRET" not in body
    assert "cron_secret" not in body
    assert VALID_SECRET not in body


def test_valid_token_returns_the_summary(client, runner):
    """A correct token in the query parameter starts a run."""
    response = client.get("/run-checks", query_string={"token": VALID_SECRET})

    assert response.status_code == 200
    assert response.get_json() == SUMMARY


def test_token_in_header_is_accepted(client, runner):
    """The token may also be sent in the X-Cron-Token header."""
    response = client.get("/run-checks", headers={"X-Cron-Token": VALID_SECRET})

    assert response.status_code == 200
    assert response.get_json() == SUMMARY


def test_get_and_post_both_work(client, runner):
    """The scheduler may use GET, a manual trigger may use POST."""
    get_response = client.get("/run-checks", query_string={"token": VALID_SECRET})
    post_response = client.post("/run-checks", query_string={"token": VALID_SECRET})

    assert get_response.status_code == 200
    assert post_response.status_code == 200
    assert get_response.get_json() == SUMMARY
    assert post_response.get_json() == SUMMARY


def test_post_accepts_the_token_in_the_header_and_in_the_body(client, runner):
    """A POST may carry the token in the header or in the form body."""
    header_response = client.post("/run-checks", headers={"X-Cron-Token": VALID_SECRET})
    body_response = client.post("/run-checks", data={"token": VALID_SECRET})

    assert header_response.status_code == 200
    assert body_response.status_code == 200


def test_runner_error_returns_500_without_a_traceback(client, monkeypatch):
    """A failing run answers 500 with a generic error."""

    def boom():
        raise RuntimeError("the runner exploded")

    monkeypatch.setattr("app.routes.run_all_checks", boom)

    response = client.get("/run-checks", query_string={"token": VALID_SECRET})

    assert response.status_code == 500
    body = response.get_data(as_text=True)
    assert "error" in response.get_json()
    assert "Traceback" not in body
    assert "exploded" not in body


def test_runner_is_not_called_without_a_valid_token(client, monkeypatch):
    """A rejected request never starts a run."""
    calls: list[int] = []
    monkeypatch.setattr("app.routes.run_all_checks", lambda: calls.append(1))

    client.get("/run-checks")
    client.get("/run-checks", query_string={"token": WRONG_TOKEN})

    assert calls == []


def test_runner_in_the_runner_module_can_be_replaced(client, monkeypatch):
    """The runner may also be replaced in app.runner."""
    monkeypatch.setattr("app.runner.run_all_checks", lambda: SUMMARY)

    response = client.get("/run-checks", query_string={"token": VALID_SECRET})

    assert response.status_code == 200
    assert response.get_json() == SUMMARY


def test_token_and_secret_never_appear_in_the_response_or_the_log(client, caplog):
    """Neither the token nor the secret may leak into a response or a log."""
    token = "a-token-that-must-not-leak"

    with caplog.at_level(logging.DEBUG):
        response = client.get("/run-checks", query_string={"token": token})

    body = response.get_data(as_text=True)
    assert response.status_code == 403
    assert token not in body
    assert VALID_SECRET not in body
    assert token not in caplog.text
    assert VALID_SECRET not in caplog.text


def test_secret_never_appears_in_the_log_when_the_runner_fails(client, monkeypatch, caplog):
    """Even the message of a failing run may not reach the log."""

    def boom():
        raise RuntimeError(f"the runner exploded with {VALID_SECRET}")

    monkeypatch.setattr("app.routes.run_all_checks", boom)

    with caplog.at_level(logging.DEBUG):
        response = client.get("/run-checks", query_string={"token": VALID_SECRET})

    body = response.get_data(as_text=True)
    assert response.status_code == 500
    assert VALID_SECRET not in body
    assert VALID_SECRET not in caplog.text
