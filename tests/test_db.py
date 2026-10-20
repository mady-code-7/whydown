"""Tests for the WhyDown database layer (app/db.py).

No real database and no network access is used: the psycopg connection is
replaced by the fake connection from ``fake_db.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import psycopg
import pytest
from fake_db import FakeConnection, fake_connect_factory

from app import db
from app.checker import CheckResult

DSN = "postgresql://whydown:hunter2@db.example.invalid:5432/whydown"


@pytest.fixture
def connection(monkeypatch):
    """A fake connection wired up as psycopg.connect and DATABASE_URL."""
    monkeypatch.setenv("DATABASE_URL", DSN)
    conn = FakeConnection()
    monkeypatch.setattr(db.psycopg, "connect", fake_connect_factory(conn))
    return conn


def sql_of(conn: FakeConnection, index: int = 0) -> str:
    """Return the SQL of the statement at ``index``."""
    return conn.statements[index].sql


# --------------------------------------------------------------------------
# Connection handling
# --------------------------------------------------------------------------

def test_connect_uses_database_url_from_env_with_connect_timeout(connection):
    """The DSN comes from the environment and a connect timeout is applied."""
    db.list_sites()

    assert len(connection.connect_calls) == 1
    conninfo, kwargs = connection.connect_calls[0]
    assert conninfo == DSN
    assert kwargs["connect_timeout"] == 10


def test_connect_without_database_url_raises_database_error(monkeypatch):
    """A missing DATABASE_URL is reported without leaking any value."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(db.DatabaseError) as excinfo:
        db.list_sites()

    assert "DATABASE_URL" in str(excinfo.value)


def test_connect_failure_message_does_not_contain_the_dsn(monkeypatch):
    """A connection failure must never expose the DSN (it holds a password)."""
    monkeypatch.setenv("DATABASE_URL", DSN)

    def exploding_connect(conninfo, **kwargs):
        raise psycopg.OperationalError(f"could not connect to server: {conninfo}")

    monkeypatch.setattr(db.psycopg, "connect", exploding_connect)

    with pytest.raises(db.DatabaseError) as excinfo:
        db.list_sites()

    message = str(excinfo.value)
    assert "hunter2" not in message
    assert DSN not in message
    assert "could not connect" in message


def test_query_failure_raises_database_error_and_rolls_back(monkeypatch):
    """A failing statement is reported as a DatabaseError and rolled back."""
    monkeypatch.setenv("DATABASE_URL", DSN)
    conn = FakeConnection([psycopg.OperationalError("relation does not exist")])
    monkeypatch.setattr(db.psycopg, "connect", fake_connect_factory(conn))

    with pytest.raises(db.DatabaseError) as excinfo:
        db.list_sites()

    assert "relation does not exist" in str(excinfo.value)
    assert conn.rollbacks == 1
    assert conn.closed is True


def test_connection_is_closed_after_every_call(connection):
    """Connections are short-lived: every call closes its connection."""
    db.list_sites()
    db.list_sites()

    assert connection.closed is True


def test_a_connection_passed_in_is_used_and_left_open(connection):
    """A caller may hand in its own connection; it is not closed for it."""
    rows = db.list_sites(conn=connection)

    assert rows == []
    assert connection.closed is False
    assert connection.connect_calls == []


def test_redact_leaves_an_empty_message_alone():
    """An empty message stays empty."""
    assert db._redact("") == ""


# --------------------------------------------------------------------------
# init_schema
# --------------------------------------------------------------------------

def test_init_schema_creates_all_tables_and_the_checks_index(connection):
    """init_schema applies the schema file: four tables plus the index."""
    db.init_schema()

    executed = " ".join(stmt.sql for stmt in connection.statements)
    for table in ("sites", "checks", "incidents", "alert_log"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in executed
    assert "CREATE INDEX IF NOT EXISTS" in executed
    assert "ON checks (site_id, checked_at" in executed
    assert connection.commits == 1


def test_init_schema_is_idempotent(connection):
    """Running init_schema twice applies the same statements again."""
    db.init_schema()
    first = [stmt.sql for stmt in connection.statements]
    db.init_schema()
    second = [stmt.sql for stmt in connection.statements[len(first):]]

    assert first == second


def test_init_schema_without_schema_file_raises_database_error(monkeypatch, connection):
    """A missing schema file is reported as a DatabaseError."""
    monkeypatch.setattr(db, "SCHEMA_PATH", Path("does/not/exist.sql"))

    with pytest.raises(db.DatabaseError) as excinfo:
        db.init_schema()

    assert "schema" in str(excinfo.value).lower()


def test_schema_file_matches_spec_section_9():
    """docs/init_db.sql holds the tables of spec section 9 and the index."""
    sql = db.SCHEMA_PATH.read_text(encoding="utf-8")

    for table in ("sites", "checks", "incidents", "alert_log"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "ON checks (site_id, checked_at" in sql
    # Every statement must be idempotent so init_schema can be re-run.
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "CREATE TABLE sites" not in sql


# --------------------------------------------------------------------------
# Sites
# --------------------------------------------------------------------------

def test_list_sites_returns_rows(connection):
    """list_sites returns the rows the database sent."""
    connection.responses = [[{"id": 1, "name": "Portal", "url": "https://a.test"}]]

    sites = db.list_sites()

    assert sites == [{"id": 1, "name": "Portal", "url": "https://a.test"}]
    assert "FROM sites" in sql_of(connection)


def test_list_sites_can_filter_to_enabled_sites(connection):
    """enabled_only adds a filter to the query."""
    db.list_sites(enabled_only=True)

    assert "WHERE enabled = TRUE" in sql_of(connection)


def test_list_sites_uses_parameterised_query(connection):
    """The site id filter is passed as a parameter, not formatted into SQL."""
    db.list_sites(enabled_only=True)

    assert sql_of(connection).count("%s") == 0


def test_add_site_inserts_and_returns_the_new_id(connection):
    """add_site inserts the site and returns its primary key."""
    connection.responses = [{"rows": [{"id": 7}], "rowcount": 1}]

    site_id = db.add_site(
        name="Portal",
        url="https://portal.test",
        expected_status=200,
        keyword="login",
        slow_threshold_ms=3000,
    )

    assert site_id == 7
    statement = connection.statements[0]
    assert "INSERT INTO sites" in statement.sql
    assert "RETURNING id" in statement.sql
    assert statement.params == ("Portal", "https://portal.test", 200, "login", 3000, True)
    assert connection.commits == 1


def test_add_site_without_returned_row_raises_database_error(connection):
    """A missing RETURNING value is an error, not a silent None."""
    connection.responses = [{"rows": [], "rowcount": 0}]

    with pytest.raises(db.DatabaseError):
        db.add_site(name="Portal", url="https://portal.test")


def test_delete_site_removes_the_site(connection):
    """delete_site removes the row and reports how many rows were removed."""
    connection.responses = [{"rows": [], "rowcount": 1}]

    assert db.delete_site(3) is True
    assert "DELETE FROM sites" in sql_of(connection)
    assert connection.statements[0].params == (3,)
    assert connection.commits == 1

    connection.responses = [{"rows": [], "rowcount": 0}]
    assert db.delete_site(99) is False


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def _sample_result() -> CheckResult:
    return CheckResult(
        dns_success=True,
        tcp_success=True,
        tls_success=True,
        http_success=True,
        dns_ms=1.5,
        tcp_ms=2.5,
        tls_ms=3.5,
        status_code=200,
        resolved_ip="93.184.216.34",
        total_ms=9.6,
        result="OK",
    )


def test_save_check_stores_the_metadata_of_a_result(connection):
    """save_check stores timings, status and result - but no body."""
    connection.responses = [{"rows": [{"id": 11}], "rowcount": 1}]

    check_id = db.save_check(4, _sample_result())

    assert check_id == 11
    statement = connection.statements[0]
    assert "INSERT INTO checks" in statement.sql
    assert "RETURNING id" in statement.sql
    params = statement.params
    assert params[0] == 4
    assert params[2] == "OK"
    assert params[3] is None  # failed_stage
    assert params[4] == 2  # dns_ms, rounded
    assert params[5] == 2  # tcp_ms, rounded
    assert params[6] == 4  # tls_ms, rounded
    assert params[7] is None  # ttfb_ms
    assert params[8] == 10  # total_ms, rounded
    assert params[9] == "93.184.216.34"
    assert connection.commits == 1


def test_save_check_accepts_a_plain_mapping(connection):
    """A result may also be given as a mapping of column names."""
    connection.responses = [{"rows": [{"id": 12}], "rowcount": 1}]

    check_id = db.save_check(4, {"result": "SLOW", "total_ms": 9000.0, "status_code": 200})

    assert check_id == 12
    assert connection.statements[0].params[2] == "SLOW"
    assert connection.statements[0].params[8] == 9000


def test_save_check_truncates_a_long_error_detail(connection):
    """Error details are capped so a check row cannot grow without bound."""
    connection.responses = [{"rows": [{"id": 13}], "rowcount": 1}]

    db.save_check(4, {"result": "INTERNAL_ERROR", "error_message": "x" * 5000})

    error_detail = connection.statements[0].params[12]
    assert len(error_detail) <= db.MAX_ERROR_DETAIL_CHARS


def test_save_check_keeps_a_short_error_detail(connection):
    """A short error message is stored unchanged."""
    connection.responses = [{"rows": [{"id": 15}], "rowcount": 1}]

    db.save_check(4, {"result": "DNS_FAILURE", "error_message": "name or service not known"})

    assert connection.statements[0].params[12] == "name or service not known"


def test_save_check_without_returned_row_raises_database_error(connection):
    """A missing RETURNING value is an error, not a silent None."""
    connection.responses = [{"rows": [], "rowcount": 0}]

    with pytest.raises(db.DatabaseError):
        db.save_check(4, {"result": "OK"})


def test_save_check_stores_the_certificate_expiry_flag(connection):
    """The cert_expiring_soon flag of spec section 8.3.1 is persisted."""
    connection.responses = [{"rows": [{"id": 14}], "rowcount": 1}]

    db.save_check(4, {"result": "OK", "cert_expiring_soon": True})

    assert connection.statements[0].params[11] is True


def test_get_recent_checks_returns_the_newest_checks_first(connection):
    """get_recent_checks returns rows ordered by time and passes the limit."""
    connection.responses = [[{"id": 2, "result": "OK"}, {"id": 1, "result": "SLOW"}]]

    checks = db.get_recent_checks(4, limit=25)

    assert [check["id"] for check in checks] == [2, 1]
    statement = connection.statements[0]
    assert "ORDER BY checked_at DESC" in statement.sql
    assert "LIMIT %s" in statement.sql
    assert statement.params == (4, 25)


def test_uptime_percent_counts_ok_slow_and_dns_slow_as_up(connection):
    """OK, SLOW and DNS_SLOW are up; every other code is down."""
    connection.responses = [{"rows": [{"total": 4, "up": 3}], "rowcount": 1}]

    uptime = db.uptime_percent(4, hours=24)

    assert uptime == 75.0
    statement = connection.statements[0]
    assert "COUNT(*)" in statement.sql
    assert "OK" in statement.params
    assert "SLOW" in statement.params
    assert "DNS_SLOW" in statement.params
    assert statement.params[-2:] == (4, 24)


def test_uptime_percent_without_any_check_is_zero(connection):
    """A window without checks has no uptime (0.0), not a division error."""
    connection.responses = [{"rows": [{"total": 0, "up": 0}], "rowcount": 1}]

    assert db.uptime_percent(4, hours=24) == 0.0


def test_purge_old_checks_deletes_rows_older_than_the_given_days(connection):
    """purge_old_checks deletes old rows and reports the number deleted."""
    connection.responses = [{"rows": [], "rowcount": 17}]

    deleted = db.purge_old_checks(days=30)

    assert deleted == 17
    statement = connection.statements[0]
    assert "DELETE FROM checks" in statement.sql
    assert statement.params == (30,)
    assert connection.commits == 1


def test_purge_old_checks_without_old_rows_returns_zero(connection):
    """Nothing to purge is not an error."""
    connection.responses = [{"rows": [], "rowcount": 0}]

    assert db.purge_old_checks() == 0


# --------------------------------------------------------------------------
# Command line entry points
# --------------------------------------------------------------------------

def test_main_without_arguments_prints_usage(capsys):
    """Running the module without a command explains what is available."""
    assert db.main([]) == 1

    assert "init" in capsys.readouterr().out


def test_main_help_prints_usage(capsys):
    """--help explains the available commands."""
    assert db.main(["--help"]) == 0

    assert "smoke" in capsys.readouterr().out


def test_main_reads_sys_argv_when_no_arguments_are_given(monkeypatch, connection, capsys):
    """Called without arguments, main takes the command from sys.argv."""
    monkeypatch.setattr("sys.argv", ["app.db", "init"])

    assert db.main() == 0

    assert "Schema is up to date" in capsys.readouterr().out


def test_main_with_unknown_command_returns_an_error_code(capsys):
    """An unknown command is reported with a non-zero exit code."""
    assert db.main(["frobnicate"]) == 2

    captured = capsys.readouterr()
    assert "frobnicate" in captured.out + captured.err


def test_main_init_runs_init_schema(connection, capsys):
    """`python -m app.db init` applies the schema."""
    assert db.main(["init"]) == 0

    assert "CREATE TABLE IF NOT EXISTS sites" in sql_of(connection)
    assert "schema" in capsys.readouterr().out.lower()


def test_main_smoke_inserts_reads_back_and_deletes_its_test_data(connection, capsys):
    """`python -m app.db smoke` writes a test row, reads it and cleans up."""
    connection.responses = [
        {"rows": [{"id": 42}], "rowcount": 1},   # add_site
        {"rows": [{"id": 99}], "rowcount": 1},   # save_check
        [{"id": 42, "name": "smoke-test", "url": "https://smoke.invalid"}],  # list_sites
        [{"id": 99, "result": "OK", "total_ms": 12}],                      # get_recent_checks
        {"rows": [{"total": 1, "up": 1}], "rowcount": 1},                   # uptime_percent
        {"rows": [], "rowcount": 1},                                       # delete_site
    ]

    assert db.main(["smoke"]) == 0

    executed = [stmt.sql for stmt in connection.statements]
    assert any("INSERT INTO sites" in sql for sql in executed)
    assert any("INSERT INTO checks" in sql for sql in executed)
    assert any("SELECT" in sql for sql in executed)
    assert any("DELETE FROM sites" in sql for sql in executed)

    out = capsys.readouterr().out
    assert "smoke-test" in out
    assert "OK" in out


def test_main_smoke_deletes_its_test_data_even_when_a_step_fails(connection, capsys):
    """The test data is removed even when the run fails half way."""
    connection.responses = [
        {"rows": [{"id": 42}], "rowcount": 1},   # add_site
        psycopg.OperationalError("checks table missing"),  # save_check fails
        {"rows": [], "rowcount": 1},             # delete_site
    ]

    assert db.main(["smoke"]) == 1

    assert any("DELETE FROM sites" in stmt.sql for stmt in connection.statements)
    captured = capsys.readouterr()
    assert "checks table missing" in captured.out + captured.err


def test_cli_never_prints_the_dsn(monkeypatch, capsys):
    """Neither a success nor a failure may print the database URL."""
    secret_url = "postgresql://whydown:sup3rs3cret@db.example.invalid/whydown"
    monkeypatch.setenv("DATABASE_URL", secret_url)

    def exploding_connect(conninfo, **kwargs):
        raise psycopg.OperationalError(f"could not connect to {conninfo}")

    monkeypatch.setattr(db.psycopg, "connect", exploding_connect)

    db.main(["init"])

    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "sup3rs3cret" not in out
    assert secret_url not in out


# --------------------------------------------------------------------------
# .env handling
# --------------------------------------------------------------------------

def test_main_loads_the_env_file_before_it_runs_a_command(connection, dotenv_calls, capsys):
    """The CLI loads .env once per invocation, before the command runs."""
    assert db.main(["init"]) == 0

    assert len(dotenv_calls) == 1
    captured = capsys.readouterr()
    assert captured.out == "Schema is up to date.\n"


def test_main_loads_the_env_file_for_the_smoke_command(connection, dotenv_calls):
    """The smoke command gets the .env file as well."""
    connection.responses = [
        {"rows": [{"id": 42}], "rowcount": 1},   # add_site
        {"rows": [{"id": 99}], "rowcount": 1},   # save_check
        [{"id": 42, "name": "smoke-test", "url": "https://smoke.invalid"}],  # list_sites
        [{"id": 99, "result": "OK", "total_ms": 12}],                      # get_recent_checks
        {"rows": [{"total": 1, "up": 1}], "rowcount": 1},                   # uptime_percent
        {"rows": [], "rowcount": 1},                                       # delete_site
    ]

    assert db.main(["smoke"]) == 0

    assert len(dotenv_calls) == 1


def test_env_file_is_not_loaded_by_the_database_functions(connection, dotenv_calls):
    """Only the command line entry point loads .env, never a db function."""
    db.list_sites()
    db.init_schema()

    assert dotenv_calls == []


def test_the_env_file_is_loaded_before_the_database_url_is_read(connection, monkeypatch):
    """The .env file is loaded before the command connects to the database."""
    order: list[str] = []

    def recording_load_dotenv(*args, **kwargs):
        order.append("load_dotenv")
        return True

    fake_connect = db.psycopg.connect

    def recording_connect(conninfo, **kwargs):
        order.append("connect")
        return fake_connect(conninfo, **kwargs)

    monkeypatch.setattr(db, "load_dotenv", recording_load_dotenv)
    monkeypatch.setattr(db.psycopg, "connect", recording_connect)

    assert db.main(["init"]) == 0

    assert order == ["load_dotenv", "connect"]


def test_the_loaded_env_file_supplies_the_database_url(connection, monkeypatch):
    """A DATABASE_URL that comes from the .env file is the one used."""
    dsn_from_env_file = "postgresql://from:dotenv@db.example.invalid/whydown"

    def fake_load_dotenv(*args, **kwargs):
        monkeypatch.setenv("DATABASE_URL", dsn_from_env_file)
        return True

    monkeypatch.setattr(db, "load_dotenv", fake_load_dotenv)

    assert db.main(["init"]) == 0

    conninfo, _kwargs = connection.connect_calls[0]
    assert conninfo == dsn_from_env_file


def test_env_file_is_not_loaded_at_import_time():
    """Importing app.db must not read a .env file (the test run stays clean)."""
    tree = ast.parse(Path(db.__file__).read_text(encoding="utf-8"))

    module_level_calls = {
        node.value.func.id
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    }

    assert "load_dotenv" not in module_level_calls
