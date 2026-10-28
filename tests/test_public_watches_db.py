"""Tests for the public watch functions of app/db.py (Phase 8, part 4A).

No real database and no network access is used: the psycopg connection is
replaced by the fake connection from ``fake_db.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fake_db import FakeConnection

from app import db


@pytest.fixture
def conn():
    """A fake connection that is handed to the functions directly."""
    return FakeConnection()


def test_create_public_watch_inserts_code_and_url_and_returns_id(conn):
    """create_public_watch stores the two columns and returns the new id."""
    conn.responses = [{"rows": [{"id": 5}], "rowcount": 1}]

    watch_id = db.create_public_watch("code123", "https://portal.test/", conn=conn)

    assert watch_id == 5
    statement = conn.statements[0]
    assert "INSERT INTO public_watches" in statement.sql
    assert "RETURNING id" in statement.sql
    assert statement.params == ("code123", "https://portal.test/")
    assert conn.commits == 1


def test_create_public_watch_without_returned_row_raises_database_error(conn):
    """A missing RETURNING value is an error, not a silent None."""
    conn.responses = [{"rows": [], "rowcount": 0}]

    with pytest.raises(db.DatabaseError):
        db.create_public_watch("code123", "https://portal.test/", conn=conn)


def test_attach_public_watch_chat_updates_and_returns_the_watch(conn):
    """attach_public_watch_chat saves the chat and returns the id and URL."""
    row = {"id": 9, "normalised_url": "https://portal.test/"}
    conn.responses = [{"rows": [row], "rowcount": 1}]

    result = db.attach_public_watch_chat("code123", "555", conn=conn)

    assert result == row
    statement = conn.statements[0]
    assert "UPDATE public_watches SET chat_id" in statement.sql
    assert statement.params == ("555", "code123", db.WATCH_HOURS)
    assert conn.commits == 1


def test_attach_public_watch_chat_unknown_code_returns_none(conn):
    """An unknown or expired code returns None."""
    conn.responses = [{"rows": [], "rowcount": 0}]

    assert db.attach_public_watch_chat("nope", "555", conn=conn) is None


def test_list_active_public_watches_returns_the_rows(conn):
    """list_active_public_watches returns the watches that have a chat."""
    rows = [
        {"id": 1, "normalised_url": "https://a.test/", "chat_id": "11"},
        {"id": 2, "normalised_url": "https://b.test/", "chat_id": "22"},
    ]
    conn.responses = [{"rows": rows, "rowcount": 2}]

    result = db.list_active_public_watches(conn=conn)

    assert result == rows
    assert "chat_id IS NOT NULL" in conn.statements[0].sql


def test_list_active_public_watches_passes_hours_and_limit_as_params(conn):
    """The window and the limit are parameters, not SQL text."""
    conn.responses = [{"rows": [], "rowcount": 0}]

    db.list_active_public_watches(limit=3, conn=conn)

    assert conn.statements[0].params == (db.WATCH_HOURS, 3)


def test_delete_public_watch_returns_true_when_a_row_went(conn):
    """delete_public_watch says True when the watch existed."""
    conn.responses = [{"rows": [], "rowcount": 1}]

    assert db.delete_public_watch(7, conn=conn) is True
    statement = conn.statements[0]
    assert "DELETE FROM public_watches" in statement.sql
    assert statement.params == (7,)
    assert conn.commits == 1


def test_delete_public_watch_returns_false_when_nothing_went(conn):
    """delete_public_watch says False when the id is unknown."""
    conn.responses = [{"rows": [], "rowcount": 0}]

    assert db.delete_public_watch(7, conn=conn) is False


def test_purge_expired_public_watches_returns_the_count(conn):
    """purge_expired_public_watches returns how many rows were deleted."""
    conn.responses = [{"rows": [], "rowcount": 3}]

    assert db.purge_expired_public_watches(conn=conn) == 3
    assert conn.statements[0].params == (db.WATCH_HOURS,)
    assert conn.commits == 1


def test_init_db_sql_has_the_public_watches_table():
    """The schema file creates the public_watches table."""
    path = Path(__file__).resolve().parent.parent / "docs" / "init_db.sql"
    text = path.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS public_watches" in text
