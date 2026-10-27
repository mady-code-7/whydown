"""Tests for the incidents page and the navigation (Phase 3C-3).

No real database and no network is used: the database layer is replaced by
fakes, so every test only checks the HTML that the route produces.

The tests also make sure that no secret (the scheduler token, the database
URL) ever shows up in a page.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from html.parser import HTMLParser

import pytest
from fake_db import FakeConnection

from app import create_app, db, routes

# Values that must never appear in a page.
SECRET_URL = "postgresql://whydown:sup3rs3cret@db.example.invalid/whydown"
SECRET_TOKEN = "0123456789abcdef"

STARTED = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
ENDED = datetime(2026, 10, 21, 13, 30, tzinfo=UTC)
# The moment the tests look at the page. An ongoing incident is as old as
# the difference between this moment and its start.
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


def summary(**overrides):
    """Return one site summary with the given values changed."""
    row = {
        "id": 1,
        "name": "Portal",
        "url": "https://portal.test",
        "enabled": True,
        "latest_result": "OK",
        "latest_checked_at": STARTED,
        "checks_24h": 10,
        "up_24h": 9,
        "checks_7d": 100,
        "up_7d": 90,
    }
    row.update(overrides)
    return row


def incident(**overrides):
    """Return one incident row with the given values changed."""
    row = {
        "id": 1,
        "site_id": 1,
        "started_at": STARTED,
        "ended_at": ENDED,
        "failure_type": "CONNECT_TIMEOUT",
        "consecutive_failures": 3,
    }
    row.update(overrides)
    return row


SITE_SUMMARIES = [
    summary(),
    summary(id=2, name="Shop", url="https://shop.test"),
]

# A closed incident of the Portal and an open one of the Shop.
INCIDENTS = [
    incident(),
    incident(
        id=2,
        site_id=2,
        started_at=datetime(2026, 10, 21, 12, 0, tzinfo=UTC),
        ended_at=None,
        failure_type="DNS_FAILURE",
        consecutive_failures=5,
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
def clock(monkeypatch):
    """Hold the clock still, so an ongoing incident has a fixed age."""
    monkeypatch.setattr(routes, "_utc_now", lambda: NOW, raising=False)


@pytest.fixture
def summaries(monkeypatch):
    """Replace ``site_summaries`` with a fixed list of rows."""
    rows = {"value": list(SITE_SUMMARIES)}

    def fake_site_summaries(*args, **kwargs):
        return list(rows["value"])

    monkeypatch.setattr(db, "site_summaries", fake_site_summaries)
    return rows


@pytest.fixture
def incidents(monkeypatch):
    """Replace ``list_incidents`` with a fixed list of rows."""
    rows = {"value": list(INCIDENTS)}

    def fake_list_incidents(*args, **kwargs):
        return list(rows["value"])

    monkeypatch.setattr(db, "list_incidents", fake_list_incidents)
    return rows


@pytest.fixture
def checks(monkeypatch):
    """Replace ``get_recent_checks`` with an empty history."""
    monkeypatch.setattr(db, "get_recent_checks", lambda *a, **k: [])


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

def test_the_incidents_page_is_rendered(client, summaries, incidents, clock):
    """The page answers with 200 and the HTML of the incidents."""
    response = client.get("/incidents")

    assert response.status_code == 200
    assert "text/html" in response.headers["Content-Type"]


def test_the_incidents_page_shows_every_incident(client, summaries, incidents, clock):
    """The page holds a row for every incident."""
    body = client.get("/incidents").get_data(as_text=True)

    assert "Portal" in body
    assert "Shop" in body


def test_the_incidents_are_shown_newest_first(client, summaries, incidents, clock):
    """The newest incident comes first (spec section 9)."""
    body = client.get("/incidents").get_data(as_text=True)

    assert body.index("Shop") < body.index("Portal")


def test_a_closed_incident_shows_its_start_in_utc(client, summaries, incidents, clock):
    """The start of an incident is shown in UTC."""
    body = client.get("/incidents").get_data(as_text=True)

    assert "2026-10-21 12:00:00 UTC" in body


def test_a_closed_incident_shows_its_end_in_utc(client, summaries, incidents, clock):
    """The end of an incident is shown in UTC."""
    body = client.get("/incidents").get_data(as_text=True)

    assert "2026-10-21 13:30:00 UTC" in body


def test_a_closed_incident_shows_its_duration(client, summaries, incidents, clock):
    """The duration of a closed incident is the time between start and end."""
    body = client.get("/incidents").get_data(as_text=True)

    assert "1h 30m 00s" in body


def test_an_open_incident_has_no_end_but_an_ongoing_badge(client, summaries, incidents, clock):
    """An incident without an end is marked as ongoing."""
    body = client.get("/incidents").get_data(as_text=True)

    assert "ongoing" in body


def test_an_open_incident_shows_the_time_since_it_started(client, summaries, incidents, clock):
    """An ongoing incident shows how long it has been going on."""
    body = client.get("/incidents").get_data(as_text=True)

    assert "21h 00m 00s" in body


def test_the_incidents_page_shows_the_failure_type(client, summaries, incidents, clock):
    """The failure type of an incident is shown."""
    body = client.get("/incidents").get_data(as_text=True)

    assert "CONNECT_TIMEOUT" in body
    assert "DNS_FAILURE" in body


def test_the_incidents_page_shows_the_number_of_failures(client, summaries, incidents, clock):
    """The number of consecutive failures of an incident is shown."""
    body = client.get("/incidents").get_data(as_text=True)

    assert ">3<" in body
    assert ">5<" in body


def test_the_name_of_a_site_links_to_its_detail_page(client, summaries, incidents, clock):
    """The name of a site is a link to the detail page of the site."""
    body = client.get("/incidents").get_data(as_text=True)

    assert 'href="/sites/1"' in body
    assert 'href="/sites/2"' in body


def test_a_site_that_is_gone_is_shown_as_its_number(client, summaries, incidents, clock, monkeypatch):
    """An incident of a site that does not exist shows the number of the site."""
    monkeypatch.setattr(db, "site_summaries", lambda *a, **k: [])
    monkeypatch.setattr(
        db,
        "list_incidents",
        lambda *a, **k: [incident(site_id=42)],
    )

    body = client.get("/incidents").get_data(as_text=True)

    assert "site #42" in body
    assert 'href="/sites/42"' not in body


def test_the_incidents_page_reads_a_hundred_incidents(client, summaries, incidents, clock, monkeypatch):
    """The page asks the database for the newest hundred incidents."""
    seen = {}

    def fake_list_incidents(*args, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(db, "list_incidents", fake_list_incidents)

    client.get("/incidents")

    assert seen.get("limit") == 100


# --------------------------------------------------------------------------
# The empty state
# --------------------------------------------------------------------------

def test_the_empty_state_says_that_there_are_no_incidents(client, summaries, monkeypatch, clock):
    """Without any incident the page says "No incidents yet"."""
    monkeypatch.setattr(db, "list_incidents", lambda *a, **k: [])

    response = client.get("/incidents")

    assert response.status_code == 200
    assert "No incidents yet" in response.get_data(as_text=True)


def test_the_empty_state_shows_no_table(client, summaries, monkeypatch, clock):
    """Without any incident there is no table."""
    monkeypatch.setattr(db, "list_incidents", lambda *a, **k: [])

    body = client.get("/incidents").get_data(as_text=True)

    assert "<table" not in body


# --------------------------------------------------------------------------
# Escaping
# --------------------------------------------------------------------------

def test_a_site_name_with_markup_is_escaped(client, summaries, monkeypatch, clock):
    """A site name can never inject markup into the page."""
    monkeypatch.setattr(
        db, "site_summaries", lambda *a, **k: [summary(name="<script>alert(1)</script>")]
    )
    monkeypatch.setattr(
        db, "list_incidents", lambda *a, **k: [incident(site_id=1)]
    )

    body = client.get("/incidents").get_data(as_text=True)

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_a_failure_type_with_markup_is_escaped(client, summaries, monkeypatch, clock):
    """A failure type can never inject markup into the page either."""
    monkeypatch.setattr(
        db,
        "list_incidents",
        lambda *a, **k: [incident(failure_type="<img src=x onerror=alert(1)>")],
    )

    body = client.get("/incidents").get_data(as_text=True)

    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body


def test_no_template_uses_the_unsafe_filter():
    """No template turns a value off with the unsafe filter."""
    from pathlib import Path

    templates = Path(routes.__file__).resolve().parent / "templates"
    for template in templates.glob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert "|safe" not in text, f"{template.name} uses the unsafe filter"


# --------------------------------------------------------------------------
# The database
# --------------------------------------------------------------------------

def test_a_request_uses_one_connection(client, summaries, incidents, clock, connection):
    """The page opens one connection and uses it for both queries."""
    client.get("/incidents")

    assert len(connection.opened) == 1


def test_a_request_closes_its_connection(client, summaries, incidents, clock, connection):
    """The connection of a request is closed again."""
    client.get("/incidents")

    assert connection.closed is True


def test_a_failed_request_closes_its_connection(client, summaries, monkeypatch, clock, connection):
    """A failing request closes its connection as well."""
    def boom(*args, **kwargs):
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "list_incidents", boom)

    client.get("/incidents")

    assert connection.closed is True


def test_a_database_failure_answers_503(client, summaries, monkeypatch, clock):
    """A failing database is answered with a 503 page."""
    def boom(*args, **kwargs):
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "list_incidents", boom)

    response = client.get("/incidents")

    assert response.status_code == 503
    assert "text/html" in response.headers["Content-Type"]


def test_the_503_page_is_the_same_page_as_the_other_pages(client, summaries, monkeypatch, clock):
    """The 503 page is the fixed error page of the dashboard."""
    def boom(*args, **kwargs):
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "list_incidents", boom)

    body = client.get("/incidents").get_data(as_text=True)

    assert "not available" in body.lower()
    assert "the database is gone" not in body


def test_a_database_failure_leaks_nothing(client, summaries, monkeypatch, clock, caplog):
    """The 503 page and the log hold neither a traceback nor the DSN."""
    def boom(*args, **kwargs):
        raise db.DatabaseError(f"could not connect to {SECRET_URL}")

    monkeypatch.setattr(db, "list_incidents", boom)

    with caplog.at_level(logging.DEBUG):
        response = client.get("/incidents")

    body = response.get_data(as_text=True)
    assert response.status_code == 503
    assert "Traceback" not in body
    assert SECRET_URL not in body
    assert "sup3rs3cret" not in body
    assert SECRET_URL not in caplog.text
    assert "sup3rs3cret" not in caplog.text


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------

def test_no_secret_appears_in_the_page(client, summaries, incidents, clock, monkeypatch):
    """Neither the token nor the database URL ends up in the page."""
    monkeypatch.setenv("CRON_SECRET", SECRET_TOKEN)
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)

    response = client.get("/incidents")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert SECRET_TOKEN not in body
    assert SECRET_URL not in body
    assert "sup3rs3cret" not in body
    assert "CRON_SECRET" not in body
    assert "DATABASE_URL" not in body


def test_an_incident_without_a_usable_site_id_says_so(client, summaries, monkeypatch, clock):
    """An incident without a site id is shown as an unknown site."""
    monkeypatch.setattr(
        db, "list_incidents", lambda *a, **k: [incident(site_id=None)]
    )

    body = client.get("/incidents").get_data(as_text=True)

    assert "unknown site" in body


def test_an_incident_without_a_start_time_still_shows_up(client, summaries, monkeypatch, clock):
    """An incident without a start time is shown, with no duration."""
    monkeypatch.setattr(
        db,
        "list_incidents",
        lambda *a, **k: [incident(started_at=None), incident(id=2, started_at=STARTED)],
    )

    response = client.get("/incidents")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "never" in body
    # The incident with a start time is the newer one, so it comes first.
    assert body.index("2026-10-21 12:00:00 UTC") < body.index("never")


# --------------------------------------------------------------------------
# The formatting helpers behind the page
# --------------------------------------------------------------------------

def test_the_clock_returns_utc():
    """``_utc_now`` returns the current time in UTC, even when it is used."""
    assert routes._utc_now().tzinfo is UTC


def test_the_duration_text_has_hours_and_minutes():
    """A minute value below an hour formats as hours and minutes."""
    assert routes._duration_text(1800) == "30m 00s"


def test_the_duration_text_falls_back_to_seconds():
    """A value shorter than a minute falls back to seconds."""
    assert routes._duration_text(42) == "42s"
    assert routes._duration_text(5) == "5s"


def test_the_duration_text_uses_days_when_longer():
    """A long duration keeps the biggest units."""
    assert routes._duration_text(2 * 86400 + 3 * 3600 + 30 * 60) == "2d 03h 30m"


def test_the_duration_text_ignores_negative_values():
    """A negative duration is treated as zero rather than going back in time."""
    assert routes._duration_text(-5) == "0s"


def test_the_elapsed_text_returns_dash_for_unknown_times():
    """A duration whose times are not dates is reported as missing."""
    assert routes._elapsed_text("not a date", "also no") == "-"
    assert routes._elapsed_text(None, None) == "-"


def test_the_elapsed_text_reads_naive_times_as_utc():
    """Naive timestamps are read as UTC, so the duration is still right."""
    start = datetime(2026, 10, 21, 12, 0)
    end = datetime(2026, 10, 21, 13, 30)
    assert routes._elapsed_text(start, end) == "1h 30m 00s"


# --------------------------------------------------------------------------
# The navigation
# --------------------------------------------------------------------------

class _NavParser(HTMLParser):
    """Collect the links of the navigation of a page."""

    def __init__(self) -> None:
        super().__init__()
        self._in_nav = False
        self._nav_depth = 0
        self._link: dict[str, object] | None = None
        self.links: list[dict[str, object]] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "nav":
            if self._in_nav:
                self._nav_depth += 1
            else:
                self._in_nav = True
                self._nav_depth = 1
            return
        if self._in_nav and tag == "a":
            self._link = {
                "href": attributes.get("href"),
                "current": attributes.get("aria-current") == "page",
                "classes": attributes.get("class", ""),
                "text": "",
            }
            self.links.append(self._link)

    def handle_endtag(self, tag):
        if tag == "a":
            self._link = None
        elif tag == "nav" and self._in_nav:
            self._nav_depth -= 1
            if self._nav_depth == 0:
                self._in_nav = False

    def handle_data(self, data):
        if self._link is not None:
            self._link["text"] = (str(self._link["text"]) + data).strip()


def nav_links(body: str) -> list[dict[str, object]]:
    """Return the links of the navigation of a page."""
    parser = _NavParser()
    parser.feed(body)
    return parser.links


def nav_link(body: str, text: str) -> dict[str, object]:
    """Return the link of the navigation with the given text."""
    for link in nav_links(body):
        if link["text"] == text:
            return link
    raise AssertionError(f"the navigation has no link {text!r}")


def test_the_navigation_has_a_link_to_the_overview(client, summaries):
    """The navigation links to the overview."""
    body = client.get("/").get_data(as_text=True)

    assert nav_link(body, "Overview")["href"] == "/"


def test_the_navigation_has_a_link_to_the_incidents(client, summaries):
    """The navigation links to the incidents."""
    body = client.get("/").get_data(as_text=True)

    assert nav_link(body, "Incidents")["href"] == "/incidents"


@pytest.mark.parametrize("path", ["/", "/sites/1", "/incidents", "/sites/999"])
def test_the_navigation_is_on_every_page(client, summaries, incidents, checks, clock, path):
    """Every page of the dashboard has the navigation."""
    body = client.get(path).get_data(as_text=True)

    assert {link["text"] for link in nav_links(body)} == {"Overview", "Incidents", "Admin"}


def test_the_overview_is_the_current_page_on_the_overview(client, summaries):
    """On the overview the overview is the current page."""
    body = client.get("/").get_data(as_text=True)

    assert nav_link(body, "Overview")["current"] is True
    assert nav_link(body, "Incidents")["current"] is False


def test_the_incidents_are_the_current_page_on_the_incidents(client, summaries, incidents, clock):
    """On the incidents page the incidents are the current page."""
    body = client.get("/incidents").get_data(as_text=True)

    assert nav_link(body, "Incidents")["current"] is True
    assert nav_link(body, "Overview")["current"] is False


def test_the_overview_is_the_current_page_on_the_site_detail(client, summaries, checks):
    """The detail page of a site is under the overview."""
    body = client.get("/sites/1").get_data(as_text=True)

    assert nav_link(body, "Overview")["current"] is True
    assert nav_link(body, "Incidents")["current"] is False


def test_the_overview_is_the_current_page_on_the_404_page(client, summaries, checks):
    """The 404 page is under the overview as well."""
    body = client.get("/sites/999").get_data(as_text=True)

    assert nav_link(body, "Overview")["current"] is True
    assert nav_link(body, "Incidents")["current"] is False


def test_the_current_page_has_a_visible_style(client, summaries):
    """The current page is marked with a style of its own."""
    body = client.get("/").get_data(as_text=True)

    assert "is-current" in nav_link(body, "Overview")["classes"]


def test_the_navigation_is_written_in_plain_css():
    """The navigation is styled by the stylesheet of the application."""
    from pathlib import Path

    style = (Path(routes.__file__).resolve().parent / "static" / "style.css").read_text(
        encoding="utf-8"
    )

    assert ".site-nav" in style
    assert ".is-current" in style
    assert "@import" not in style
