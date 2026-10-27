"""Tests for the dashboard overview page (Phase 3C-1).

No real database and no network is used: the database layer is replaced by
fakes, so every test only checks the HTML that the route produces.

The tests also make sure that no secret (the scheduler token, the database
URL) ever shows up in a page.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from fake_db import FakeConnection

from app import create_app, db

# Values that must never appear in a page.
SECRET_URL = "postgresql://whydown:sup3rs3cret@db.example.invalid/whydown"
SECRET_TOKEN = "0123456789abcdef"

LATEST = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def summary(**overrides):
    """Return one site summary with the given values changed."""
    row = {
        "id": 1,
        "name": "Portal",
        "url": "https://portal.test",
        "enabled": True,
        "latest_result": "OK",
        "latest_checked_at": LATEST,
        "checks_24h": 10,
        "up_24h": 9,
        "checks_7d": 100,
        "up_7d": 90,
    }
    row.update(overrides)
    return row


SITE_SUMMARIES = [
    summary(),
    summary(
        id=2,
        name="Shop",
        url="https://shop.test",
        latest_result=None,
        latest_checked_at=None,
        checks_24h=0,
        up_24h=0,
        checks_7d=0,
        up_7d=0,
    ),
]


@pytest.fixture
def connection(monkeypatch):
    """Replace the database connection with a fake one.

    The counter counts how often a connection was opened, so a test can
    prove that a request needs only one.
    """
    conn = FakeConnection()
    opened: list[FakeConnection] = []

    def fake_connect():
        opened.append(conn)
        return conn

    monkeypatch.setattr(db, "_connect", fake_connect)
    conn.opened = opened  # type: ignore[attr-defined]
    return conn


@pytest.fixture
def client(connection, monkeypatch):
    """A test client whose database layer is faked and that is logged in as an admin."""
    from app import auth

    monkeypatch.setattr(auth, "admin_configured", lambda: True)
    monkeypatch.setattr(auth, "is_admin", lambda: True)
    application = create_app()
    application.config["TESTING"] = True
    return application.test_client()


@pytest.fixture
def summaries(monkeypatch):
    """Replace ``site_summaries`` with a fixed list of rows."""
    rows = {"value": list(SITE_SUMMARIES)}

    def fake_site_summaries(*args, **kwargs):
        return list(rows["value"])

    monkeypatch.setattr(db, "site_summaries", fake_site_summaries)
    return rows


# --------------------------------------------------------------------------
# The page with sites
# --------------------------------------------------------------------------

def test_the_overview_page_is_rendered(client, summaries):
    """The page answers with 200 and the HTML of the overview."""
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["Content-Type"]


def test_the_overview_page_shows_every_site(client, summaries):
    """The name and the URL of every site are on the page."""
    body = client.get("/").get_data(as_text=True)

    assert "Portal" in body
    assert "https://portal.test" in body
    assert "Shop" in body
    assert "https://shop.test" in body


def test_the_overview_page_shows_the_uptime_in_percent(client, summaries):
    """The uptime of the 24 h and the 7 d window is a percentage."""
    body = client.get("/").get_data(as_text=True)

    assert "90%" in body


def test_the_overview_page_shows_the_last_check_time_in_utc(client, summaries):
    """The time of the latest check is shown in UTC."""
    body = client.get("/").get_data(as_text=True)

    assert "2026-10-21 12:00:00 UTC" in body


def test_the_name_of_a_site_links_to_its_detail_page(client, summaries):
    """The name of a site is a link to the detail page of the site."""
    body = client.get("/").get_data(as_text=True)

    assert 'href="/sites/1"' in body
    assert 'href="/sites/2"' in body


def test_the_link_of_a_site_holds_its_name(client, summaries):
    """The link holds the name of the site, escaped as usual."""
    body = client.get("/").get_data(as_text=True)

    assert '<a href="/sites/1">Portal</a>' in body


def test_the_name_of_a_site_with_markup_is_escaped_in_the_link(client, monkeypatch):
    """A site name can never inject markup into the link of the overview."""
    monkeypatch.setattr(
        db,
        "site_summaries",
        lambda *a, **k: [summary(name='<script>alert(1)</script>')],
    )

    body = client.get("/").get_data(as_text=True)

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert 'href="/sites/1"' in body


def test_a_naive_check_time_is_read_as_utc(client, monkeypatch):
    """A check time without a time zone is read as UTC."""
    monkeypatch.setattr(
        db,
        "site_summaries",
        lambda *a, **k: [summary(latest_checked_at=datetime(2026, 10, 21, 12, 0))],
    )

    body = client.get("/").get_data(as_text=True)

    assert "2026-10-21 12:00:00 UTC" in body


def test_a_check_time_in_another_time_zone_is_converted_to_utc(client, monkeypatch):
    """A check time in another time zone is converted to UTC."""
    from datetime import timedelta, timezone

    other = timezone(timedelta(hours=5))
    monkeypatch.setattr(
        db,
        "site_summaries",
        lambda *a, **k: [summary(latest_checked_at=datetime(2026, 10, 21, 12, 0, tzinfo=other))],
    )

    body = client.get("/").get_data(as_text=True)

    assert "2026-10-21 07:00:00 UTC" in body


def test_a_value_that_is_not_a_date_is_left_alone(client, monkeypatch):
    """A value that is not a date is passed through unchanged."""
    monkeypatch.setattr(
        db, "site_summaries", lambda *a, **k: [summary(latest_checked_at="not a date")]
    )

    body = client.get("/").get_data(as_text=True)

    assert "not a date" in body


def test_a_site_without_a_check_shows_that_it_was_never_checked(client, monkeypatch):
    """A site without any check says so."""
    monkeypatch.setattr(
        db, "site_summaries", lambda *a, **k: [summary(latest_result=None, latest_checked_at=None)]
    )

    body = client.get("/").get_data(as_text=True)

    assert "never" in body


def test_the_overview_page_shows_a_summary_strip(client, summaries):
    """The strip shows the number of sites and how many are up or down."""
    body = client.get("/").get_data(as_text=True)

    assert "Sites monitored" in body
    assert "Up now" in body
    assert "Down now" in body


def test_the_summary_strip_counts_the_up_and_the_down_sites(client, summaries):
    """One site is up, none is down and one has no check yet."""
    body = client.get("/").get_data(as_text=True)

    tile_values = _tile_values(body)
    assert tile_values == ["2", "1", "0"]


def test_a_slow_site_counts_as_up(client, monkeypatch):
    """A slow but working site is up (spec section 8.3)."""
    monkeypatch.setattr(db, "site_summaries", lambda *a, **k: [summary(latest_result="SLOW")])

    body = client.get("/").get_data(as_text=True)

    assert _tile_values(body) == ["1", "1", "0"]


def test_a_failing_site_counts_as_down(client, monkeypatch):
    """A site with a failure result is down."""
    monkeypatch.setattr(
        db, "site_summaries", lambda *a, **k: [summary(latest_result="CONNECT_TIMEOUT")]
    )

    body = client.get("/").get_data(as_text=True)

    assert _tile_values(body) == ["1", "0", "1"]


def test_a_site_without_a_check_is_neither_up_nor_down(client, monkeypatch):
    """A site that was never checked is not reported as a problem."""
    monkeypatch.setattr(
        db, "site_summaries", lambda *a, **k: [summary(latest_result=None, latest_checked_at=None)]
    )

    body = client.get("/").get_data(as_text=True)

    assert _tile_values(body) == ["1", "0", "0"]
    assert "without a check yet" in body


def _tile_values(body: str) -> list[str]:
    """Return the three numbers of the summary strip, in order."""
    values = []
    for chunk in body.split('class="tile-value"')[1:]:
        values.append(chunk.split(">")[1].split("<")[0].strip())
    return values


# --------------------------------------------------------------------------
# The empty state
# --------------------------------------------------------------------------

def test_the_empty_state_says_that_there_are_no_sites(client, monkeypatch):
    """Without any site the page says "No sites yet"."""
    monkeypatch.setattr(db, "site_summaries", lambda *a, **k: [])

    response = client.get("/")

    assert response.status_code == 200
    assert "No sites yet" in response.get_data(as_text=True)


def test_the_empty_state_shows_no_table(client, monkeypatch):
    """Without any site there is no table of sites."""
    monkeypatch.setattr(db, "site_summaries", lambda *a, **k: [])

    body = client.get("/").get_data(as_text=True)

    assert "<table" not in body


def test_the_empty_state_still_shows_the_summary_strip(client, monkeypatch):
    """The strip is there even when there is nothing to show."""
    monkeypatch.setattr(db, "site_summaries", lambda *a, **k: [])

    body = client.get("/").get_data(as_text=True)

    assert _tile_values(body) == ["0", "0", "0"]


# --------------------------------------------------------------------------
# The uptime without any check
# --------------------------------------------------------------------------

def test_a_window_without_checks_shows_no_data(client, monkeypatch):
    """A window without a check has no uptime: "no data", not 0%."""
    monkeypatch.setattr(
        db,
        "site_summaries",
        lambda *a, **k: [
            summary(checks_24h=0, up_24h=0, checks_7d=0, up_7d=0),
        ],
    )

    body = client.get("/").get_data(as_text=True)

    assert "no data" in body
    assert "0%" not in body


def test_a_window_without_up_checks_shows_zero_percent(client, monkeypatch):
    """A window with checks but none of them up really is 0 percent."""
    monkeypatch.setattr(
        db, "site_summaries", lambda *a, **k: [summary(checks_24h=4, up_24h=0)]
    )

    body = client.get("/").get_data(as_text=True)

    assert "0%" in body
    assert "no data" not in body


def test_the_uptime_is_shown_with_two_decimals(client, monkeypatch):
    """A third of the checks up are 33.33 percent."""
    monkeypatch.setattr(db, "site_summaries", lambda *a, **k: [summary(checks_24h=3, up_24h=1)])

    body = client.get("/").get_data(as_text=True)

    assert "33.33%" in body


# --------------------------------------------------------------------------
# The status badge
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("result", "badge_class"),
    [
        ("OK", "badge-ok"),
        ("SLOW", "badge-slow"),
        ("DNS_SLOW", "badge-slow"),
        ("CONNECT_TIMEOUT", "badge-down"),
        ("DNS_FAILURE", "badge-down"),
        ("TLS_ERROR", "badge-down"),
        ("CERT_EXPIRED", "badge-down"),
        ("HTTP_5XX", "badge-down"),
        ("KEYWORD_MISSING", "badge-down"),
        ("INTERNAL_ERROR", "badge-down"),
    ],
)
def test_the_badge_of_a_result(client, monkeypatch, result, badge_class):
    """Every result code gets its badge class."""
    monkeypatch.setattr(db, "site_summaries", lambda *a, **k: [summary(latest_result=result)])

    body = client.get("/").get_data(as_text=True)

    assert badge_class in body
    assert f">{result}<" in body


def test_a_site_without_a_check_gets_the_grey_badge(client, monkeypatch):
    """A site without any check is grey, not red."""
    monkeypatch.setattr(
        db, "site_summaries", lambda *a, **k: [summary(latest_result=None, latest_checked_at=None)]
    )

    body = client.get("/").get_data(as_text=True)

    assert "badge-none" in body
    assert "badge-down" not in body
    assert "no checks yet" in body


def test_a_slow_result_is_not_green(client, monkeypatch):
    """A slow site is amber, not green."""
    monkeypatch.setattr(db, "site_summaries", lambda *a, **k: [summary(latest_result="SLOW")])

    body = client.get("/").get_data(as_text=True)

    assert "badge-slow" in body
    assert "badge-ok" not in body


# --------------------------------------------------------------------------
# Escaping
# --------------------------------------------------------------------------

def test_a_site_name_with_markup_is_escaped(client, monkeypatch):
    """A site name can never inject markup into the page."""
    monkeypatch.setattr(
        db,
        "site_summaries",
        lambda *a, **k: [summary(name="<script>alert(1)</script>")],
    )

    response = client.get("/")
    body = response.get_data(as_text=True)

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_a_site_url_with_markup_is_escaped(client, monkeypatch):
    """A stored URL is escaped as well."""
    monkeypatch.setattr(
        db,
        "site_summaries",
        lambda *a, **k: [summary(url='https://x.test/"onmouseover="alert(1)')],
    )

    body = client.get("/").get_data(as_text=True)

    assert 'onmouseover="alert(1)' not in body
    assert "&#34;" in body


def test_the_page_escapes_every_value(client, monkeypatch):
    """The template escapes every value it prints."""
    monkeypatch.setattr(db, "site_summaries", lambda *a, **k: [summary(name="A & B")])

    body = client.get("/").get_data(as_text=True)

    assert "A & B" not in body
    assert "A &amp; B" in body


# --------------------------------------------------------------------------
# The database
# --------------------------------------------------------------------------

def test_a_request_uses_one_connection(client, summaries, connection):
    """The page opens one connection and uses it for the query."""
    client.get("/")

    assert len(connection.opened) == 1


def test_a_request_closes_its_connection(client, summaries, connection):
    """The connection of a request is closed again."""
    client.get("/")

    assert connection.closed is True


def test_a_failed_request_closes_its_connection(client, monkeypatch, connection):
    """A failing request closes its connection as well."""
    def boom(*args, **kwargs):
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "site_summaries", boom)

    client.get("/")

    assert connection.closed is True


def test_a_database_failure_answers_503(client, monkeypatch):
    """A failing database is answered with a 503 page."""
    def boom(*args, **kwargs):
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "site_summaries", boom)

    response = client.get("/")

    assert response.status_code == 503
    assert "text/html" in response.headers["Content-Type"]


def test_a_failing_connection_answers_503(client, monkeypatch):
    """A database that cannot be reached is answered with a 503."""
    def boom():
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "_connect", boom)

    response = client.get("/")

    assert response.status_code == 503


def test_the_503_page_holds_a_fixed_message(client, monkeypatch):
    """The 503 page says that the dashboard is not available."""
    def boom(*args, **kwargs):
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "site_summaries", boom)

    body = client.get("/").get_data(as_text=True)

    assert "not available" in body.lower()
    assert "the database is gone" not in body


def test_a_database_failure_leaks_nothing(client, monkeypatch, caplog):
    """The 503 page and the log hold neither a traceback nor the DSN."""
    def boom(*args, **kwargs):
        raise db.DatabaseError(f"could not connect to {SECRET_URL}")

    monkeypatch.setattr(db, "site_summaries", boom)

    with caplog.at_level(logging.DEBUG):
        response = client.get("/")

    body = response.get_data(as_text=True)
    assert response.status_code == 503
    assert "Traceback" not in body
    assert SECRET_URL not in body
    assert "sup3rs3cret" not in body
    assert "DATABASE_URL" not in body
    assert SECRET_URL not in caplog.text
    assert "sup3rs3cret" not in caplog.text


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------

def test_no_secret_appears_in_the_page(client, summaries, monkeypatch):
    """Neither the token nor the database URL ends up in the page."""
    monkeypatch.setenv("CRON_SECRET", SECRET_TOKEN)
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)

    response = client.get("/")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert SECRET_TOKEN not in body
    assert SECRET_URL not in body
    assert "sup3rs3cret" not in body
    assert "CRON_SECRET" not in body
    assert "DATABASE_URL" not in body


def test_no_secret_appears_in_the_503_page(client, monkeypatch):
    """The error page holds no secret either."""
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)

    def boom(*args, **kwargs):
        raise db.DatabaseError(f"could not connect to {SECRET_URL}")

    monkeypatch.setattr(db, "site_summaries", boom)

    body = client.get("/").get_data(as_text=True)

    assert SECRET_URL not in body
    assert "sup3rs3cret" not in body


# --------------------------------------------------------------------------
# The page and the real database function together
# --------------------------------------------------------------------------

def test_the_page_works_with_the_real_database_function(monkeypatch):
    """The page and the real database function fit together.

    Only the connection is faked here, so the SQL, the column names and the
    row format of the database layer are used as they are in production.
    """
    conn = FakeConnection([
        # site_summaries: the sites, then the counts
        [
            {
                "id": 1,
                "name": "Portal",
                "url": "https://portal.test",
                "enabled": True,
                "latest_result": "OK",
                "latest_checked_at": LATEST,
            }
        ],
        [
            {
                "site_id": 1,
                "checks_24h": 4,
                "up_24h": 3,
                "checks_7d": 40,
                "up_7d": 30,
            }
        ],
    ])
    monkeypatch.setattr(db, "_connect", lambda: conn)

    from app import auth

    monkeypatch.setattr(auth, "admin_configured", lambda: True)
    monkeypatch.setattr(auth, "is_admin", lambda: True)

    application = create_app()
    application.config["TESTING"] = True
    response = application.test_client().get("/")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Portal" in body
    assert "75%" in body
    assert "2026-10-21 12:00:00 UTC" in body
    assert "badge-ok" in body
    assert conn.closed is True
