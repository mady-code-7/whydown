"""A fake psycopg connection used by the database layer tests.

The fake mimics the small part of the psycopg 3 API that ``app/db.py`` uses:
a connection hands out cursors, a cursor executes (parameterised) statements
and returns rows, and the connection can be committed, rolled back and closed.

No real database is ever contacted. Responses are handed out in the order the
statements are executed; an entry may be

* a list of rows          -> returned by ``fetchall()``/``fetchone()``
* a dict                  -> ``{"rows": [...], "rowcount": n}``
* an exception instance   -> raised by ``execute()``
"""

from __future__ import annotations

from typing import Any, Self


class Statement:
    """One executed statement together with its parameters."""

    def __init__(self, sql: str, params: Any = None) -> None:
        self.sql = sql
        self.params = params

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"Statement({self.sql!r}, {self.params!r})"


class FakeCursor:
    """Minimal stand-in for ``psycopg.Cursor``."""

    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.rows: list[Any] = []
        self.rowcount: int = 0
        self.closed = False

    def execute(self, query: str, params: Any = None) -> FakeCursor:
        self.connection.statements.append(Statement(query, params))
        response = self.connection.next_response()
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict):
            self.rows = list(response.get("rows", []))
            self.rowcount = int(response.get("rowcount", len(self.rows)))
        else:
            self.rows = list(response)
            self.rowcount = len(self.rows)
        return self

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return list(self.rows)

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False


class FakeConnection:
    """Minimal stand-in for ``psycopg.Connection``."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.statements: list[Statement] = []
        self.connect_calls: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def next_response(self) -> Any:
        """Return the next queued response (empty result by default)."""
        if self.responses:
            return self.responses.pop(0)
        return []

    def cursor(self, row_factory: Any = None) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False


def fake_connect_factory(connection: FakeConnection):
    """Return a replacement for ``psycopg.connect`` that yields ``connection``.

    The returned callable records the connection string and the keyword
    arguments it was called with, so a test can assert that the DSN comes from
    the environment and that a connect timeout is applied.
    """

    def fake_connect(conninfo: str, **kwargs: Any) -> FakeConnection:
        connection.connect_calls.append((conninfo, kwargs))
        return connection

    return fake_connect
