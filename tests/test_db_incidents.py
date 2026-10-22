"""Tests for the incident database helpers of app/db.py.

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


def sql_of(conn: FakeConnection, index: int = 0) -> str:
    """Return the SQL of the statement at ``index``."""
    return conn.statements[index].sql


# --------------------------------------------------------------------------
# get_open_incident
# --------------------------------------------------------------------------

def test_get_open_incident_returns_the_open_incident(connection):
    """The open incident of a site is returned as a row."""
    connection.responses = [[{
        "id": 5,
        "site_id": 3,
        "started_at": "2026-10-22 10:00:00",
        "ended_at": None,
        "failure_type": "HTTP_5XX",
        "consecutive_failures": 3,
    }]]

    incident = db.get_open_incident(3)

    assert incident["id"] == 5
    assert incident["failure_type"] == "HTTP_5XX"
    assert incident["ended_at"] is None


def test_get_open_incident_without_an_open_incident_returns_none(connection):
    """A site without an open incident has none."""
    connection.responses = [[]]

    assert db.get_open_incident(3) is None


def test_get_open_incident_only_looks_at_open_incidents(connection):
    """Only the incidents without an end are taken into account."""
    db.get_open_incident(3)

    statement = connection.statements[0]
    assert "FROM incidents" in statement.sql
    assert "ended_at IS NULL" in statement.sql
    assert statement.params == (3,)


def test_get_open_incident_uses_a_parameterised_site_id(connection):
    """The site id is passed as a parameter, never formatted into the SQL."""
    db.get_open_incident(3)

    assert connection.statements[0].params == (3,)
    assert "3" not in connection.statements[0].sql


def test_get_open_incident_uses_a_given_connection(connection):
    """A caller may hand in its own connection."""
    db.get_open_incident(3, conn=connection)

    assert connection.connect_calls == []
    assert connection.closed is False


# --------------------------------------------------------------------------
# open_incident
# --------------------------------------------------------------------------

def test_open_incident_stores_the_incident(connection):
    """An incident is stored with its site, failure type and failure count."""
    connection.responses = [{"rows": [{"id": 9}], "rowcount": 1}]

    incident_id = db.open_incident(3, failure_type="HTTP_5XX", consecutive_failures=4)

    assert incident_id == 9
    statement = connection.statements[0]
    assert "INSERT INTO incidents" in statement.sql
    assert "RETURNING id" in statement.sql
    assert statement.params == (3, "HTTP_5XX", 4)
    assert connection.commits == 1


def test_open_incident_sets_the_start_to_the_current_time(connection):
    """The incident begins when it is opened, so the database sets the time."""
    connection.responses = [{"rows": [{"id": 9}], "rowcount": 1}]

    db.open_incident(3, failure_type="HTTP_5XX")

    assert "NOW()" in sql_of(connection)


def test_open_incident_without_a_returned_row_raises_database_error(connection):
    """A missing RETURNING value is an error, not a silent None."""
    connection.responses = [{"rows": [], "rowcount": 0}]

    with pytest.raises(db.DatabaseError):
        db.open_incident(3, failure_type="HTTP_5XX")


# --------------------------------------------------------------------------
# close_incident
# --------------------------------------------------------------------------

def test_close_incident_sets_the_end(connection):
    """Closing an incident gives it an end time."""
    connection.responses = [{"rows": [], "rowcount": 1}]

    assert db.close_incident(9) is True

    statement = connection.statements[0]
    assert "UPDATE incidents" in statement.sql
    assert "ended_at = NOW()" in statement.sql
    assert statement.params == (9,)
    assert connection.commits == 1


def test_close_incident_only_closes_an_open_incident(connection):
    """An incident that has ended before is not closed again."""
    connection.responses = [{"rows": [], "rowcount": 0}]

    assert db.close_incident(9) is False

    assert "ended_at IS NULL" in sql_of(connection)


# --------------------------------------------------------------------------
# recent_results
# --------------------------------------------------------------------------

def test_recent_results_returns_the_codes_newest_first(connection):
    """The result codes of the latest checks come back newest first."""
    connection.responses = [[{"result": "HTTP_5XX"}, {"result": "OK"}, {"result": "SLOW"}]]

    codes = db.recent_results(3, limit=5)

    assert codes == ["HTTP_5XX", "OK", "SLOW"]


def test_recent_results_reads_only_the_site_and_the_limit(connection):
    """Only the site id and the limit are read - no other column."""
    db.recent_results(3, limit=5)

    statement = connection.statements[0]
    assert "SELECT result FROM checks" in statement.sql
    assert "ORDER BY checked_at DESC" in statement.sql
    assert statement.params == (3, 5)


def test_recent_results_without_any_check_is_empty(connection):
    """A site without checks has no result codes."""
    connection.responses = [[]]

    assert db.recent_results(3) == []


def test_recent_results_uses_a_given_connection(connection):
    """A caller may hand in its own connection."""
    db.recent_results(3, conn=connection)

    assert connection.connect_calls == []
    assert connection.closed is False
