"""Tests for the alert helpers of app/db.py.

No real database is used: the psycopg connection is replaced by the fake
connection from ``fake_db.py``.
"""

from __future__ import annotations

import pytest
from fake_db import FakeConnection, fake_connect_factory

from app import db


@pytest.fixture
def connection(monkeypatch):
    """A fake connection wired up as psycopg.connect and DATABASE_URL."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://whydown:hunter2@db.invalid/whydown")
    conn = FakeConnection()
    monkeypatch.setattr(db.psycopg, "connect", fake_connect_factory(conn))
    return conn


# --------------------------------------------------------------------------
# log_alert
# --------------------------------------------------------------------------

def test_log_alert_stores_the_attempt(connection):
    """An attempt is stored with its incident, its site, its kind and its channel."""
    connection.responses = [{"rows": [{"id": 4}], "rowcount": 1}]

    alert_id = db.log_alert(site_id=3, incident_id=7, kind="DOWN", success=True)

    assert alert_id == 4
    statement = connection.statements[0]
    assert "INSERT INTO alert_log" in statement.sql
    assert statement.params == (7, 3, "DOWN", "telegram", True)
    assert connection.commits == 1


def test_log_alert_sets_the_time_of_the_attempt(connection):
    """The attempt is logged with the time of the database."""
    connection.responses = [{"rows": [{"id": 4}], "rowcount": 1}]

    db.log_alert(site_id=3, incident_id=7, kind="RECOVERY")

    assert "NOW()" in connection.statements[0].sql


def test_log_alert_stores_a_failed_attempt(connection):
    """A failed attempt is stored as well."""
    connection.responses = [{"rows": [{"id": 4}], "rowcount": 1}]

    db.log_alert(site_id=3, incident_id=7, kind="DOWN", success=False)

    assert connection.statements[0].params == (7, 3, "DOWN", "telegram", False)


def test_log_alert_uses_a_parameterised_incident_id(connection):
    """The incident id is passed as a parameter, never formatted into the SQL."""
    connection.responses = [{"rows": [{"id": 4}], "rowcount": 1}]

    db.log_alert(site_id=3, incident_id=7, kind="DOWN")

    assert "7" not in connection.statements[0].sql


def test_log_alert_without_a_returned_row_raises_database_error(connection):
    """A missing RETURNING value is an error, not a silent None."""
    connection.responses = [{"rows": [], "rowcount": 0}]

    with pytest.raises(db.DatabaseError):
        db.log_alert(site_id=3, incident_id=7, kind="DOWN")


# --------------------------------------------------------------------------
# alert_exists
# --------------------------------------------------------------------------

def test_alert_exists_finds_a_logged_alert(connection):
    """An alert that is in the log is found."""
    connection.responses = [[{"id": 1}]]

    assert db.alert_exists(7, "DOWN") is True


def test_alert_exists_is_false_without_an_alert(connection):
    """An incident without an alert of that kind is not found."""
    connection.responses = [[]]

    assert db.alert_exists(7, "DOWN") is False


def test_alert_exists_asks_for_the_incident_and_the_kind(connection):
    """Only the incident and the kind are read."""
    db.alert_exists(7, "DOWN")

    statement = connection.statements[0]
    assert "FROM alert_log" in statement.sql
    assert statement.params == (7, "DOWN")
    assert "7" not in statement.sql


# --------------------------------------------------------------------------
# get_latest_incident
# --------------------------------------------------------------------------

def test_get_latest_incident_returns_the_newest_incident(connection):
    """The newest incident of a site is returned as a row."""
    connection.responses = [[{
        "id": 9,
        "site_id": 3,
        "started_at": "2026-10-22 10:00:00",
        "ended_at": None,
        "failure_type": "HTTP_5XX",
        "consecutive_failures": 3,
    }]]

    incident = db.get_latest_incident(3)

    assert incident["id"] == 9
    assert incident["failure_type"] == "HTTP_5XX"


def test_get_latest_incident_reads_the_newest_one_first(connection):
    """The newest incident of a site is the first one."""
    db.get_latest_incident(3)

    statement = connection.statements[0]
    assert "FROM incidents" in statement.sql
    assert "ORDER BY started_at DESC" in statement.sql
    assert statement.params == (3,)


def test_get_latest_incident_without_an_incident_returns_none(connection):
    """A site without an incident has none."""
    connection.responses = [[]]

    assert db.get_latest_incident(3) is None


def test_get_latest_incident_uses_a_given_connection(connection):
    """A caller may hand in its own connection."""
    db.get_latest_incident(3, conn=connection)

    assert connection.connect_calls == []
    assert connection.closed is False
