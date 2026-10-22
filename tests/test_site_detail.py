"""Tests for the site detail page (Phase 3C-2).

No real database and no network is used: the database layer is replaced by
fakes, so every test only checks the HTML that the route produces.

The tests also make sure that no secret (the scheduler token, the database
URL) ever shows up in a page and that no value of the database can inject
markup into the page or into the JSON element of the charts.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from html.parser import HTMLParser

VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _CanvasAncestors(HTMLParser):
    """Track, for every <canvas> start tag, the list of ancestor element classes."""

    def __init__(self) -> None:
        super().__init__()
        self._stack: list[str] = []
        self._results: list[tuple[list[str], dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str]]) -> None:
        attributes = dict(attrs)
        if tag == "canvas":
            # Record the ancestor classes that are open right now (before pushing
            # this canvas element onto the stack).
            self._results.append((list(self._stack), dict(attributes)))
        if tag not in VOID_TAGS:
            classes = [c for c in attributes.get("class", "").split() if c]
            if classes:
                self._stack.append(" ".join(classes))

    def handle_endtag(self, tag: str) -> None:
        if tag not in VOID_TAGS and self._stack:
            self._stack.pop()

    def results(self) -> list[tuple[list[str], dict[str, str]]]:
        return self._results


import pytest
from fake_db import FakeConnection

from app import create_app, db

# Values that must never appear in a page.
SECRET_URL = "postgresql://whydown:sup3rs3cret@db.example.invalid/whydown"
SECRET_TOKEN = "0123456789abcdef"

# The pinned version of the chart library. It is never "@latest".
CHART_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.js"

LATEST = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
EARLIER = datetime(2026, 10, 21, 11, 50, tzinfo=UTC)
OLDEST = datetime(2026, 10, 21, 11, 40, tzinfo=UTC)

CERT_EXPIRY = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)


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


def check(**overrides):
    """Return one check row with the given values changed."""
    row = {
        "id": 1,
        "site_id": 1,
        "checked_at": LATEST,
        "status_code": 200,
        "result": "OK",
        "failed_stage": None,
        "dns_ms": 10,
        "tcp_ms": 20,
        "tls_ms": 30,
        "ttfb_ms": 40,
        "total_ms": 100,
        "resolved_ip": "203.0.113.5",
        "cert_expires_at": CERT_EXPIRY,
        "cert_expiring_soon": False,
        "error_detail": None,
    }
    row.update(overrides)
    return row


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
def client(connection):
    """A test client whose database layer is faked."""
    application = create_app()
    application.config["TESTING"] = True
    return application.test_client()


@pytest.fixture
def summaries(monkeypatch):
    """Replace ``site_summaries`` with a fixed list of rows."""
    rows = {"value": [summary()]}

    def fake_site_summaries(*args, **kwargs):
        return list(rows["value"])

    monkeypatch.setattr(db, "site_summaries", fake_site_summaries)
    return rows


@pytest.fixture
def checks(monkeypatch):
    """Replace ``get_recent_checks`` with a fixed list of rows.

    The rows are newest first, exactly as the database layer returns them.
    """
    rows = {"value": [check()]}

    def fake_get_recent_checks(*args, **kwargs):
        return list(rows["value"])

    monkeypatch.setattr(db, "get_recent_checks", fake_get_recent_checks)
    return rows


def chart_element(body: str) -> str:
    """Return the raw content of the JSON element of the charts."""
    match = re.search(
        r'<script type="application/json" id="chart-data">(.*?)</script>',
        body,
        re.DOTALL,
    )
    assert match, "the page has no JSON element for the charts"
    return match.group(1)


def chart_data(body: str) -> dict:
    """Return the parsed content of the JSON element of the charts."""
    return json.loads(chart_element(body))


def script_tags(body: str) -> list[tuple[str, str]]:
    """Return every script tag of the page as (attributes, content)."""
    return re.findall(r"<script([^>]*)>(.*?)</script>", body, re.DOTALL)


# --------------------------------------------------------------------------
# The page with checks
# --------------------------------------------------------------------------

def test_the_page_is_rendered(client, summaries, checks):
    """The page answers with 200 and the HTML of the detail page."""
    response = client.get("/sites/1")

    assert response.status_code == 200
    assert "text/html" in response.headers["Content-Type"]


def test_the_page_shows_the_name_and_the_url(client, summaries, checks):
    """The name and the URL of the site are on the page."""
    body = client.get("/sites/1").get_data(as_text=True)

    assert "Portal" in body
    assert "https://portal.test" in body


def test_the_page_shows_the_status_badge(client, summaries, checks):
    """The badge of the site follows the rules of the overview."""
    body = client.get("/sites/1").get_data(as_text=True)

    assert "badge-ok" in body
    assert ">OK<" in body


def test_a_failing_site_gets_the_red_badge(client, summaries, checks, monkeypatch):
    """A site with a failure result is shown as down."""
    monkeypatch.setattr(
        db, "site_summaries", lambda *a, **k: [summary(latest_result="CONNECT_TIMEOUT")]
    )

    body = client.get("/sites/1").get_data(as_text=True)

    assert "badge-down" in body
    assert ">CONNECT_TIMEOUT<" in body


def test_the_page_shows_the_uptime(client, summaries, checks):
    """The uptime of the 24 h and the 7 d window is a percentage."""
    body = client.get("/sites/1").get_data(as_text=True)

    assert "90%" in body
    assert "Uptime 24 h" in body
    assert "Uptime 7 d" in body


def test_a_window_without_checks_shows_no_data(client, summaries, checks, monkeypatch):
    """A window without a check has no uptime: "no data", not 0%."""
    monkeypatch.setattr(
        db,
        "site_summaries",
        lambda *a, **k: [summary(checks_24h=0, up_24h=0, checks_7d=0, up_7d=0)],
    )

    body = client.get("/sites/1").get_data(as_text=True)

    assert "no data" in body
    assert "0%" not in body


def test_the_page_shows_the_certificate_expiry(client, summaries, checks):
    """The expiry date of the certificate is on the page."""
    body = client.get("/sites/1").get_data(as_text=True)

    assert "2026-12-01" in body
    assert "expiring soon" not in body


def test_a_certificate_that_expires_soon_gets_a_badge(client, summaries, checks, monkeypatch):
    """A certificate that expires soon says so."""
    monkeypatch.setattr(
        db,
        "get_recent_checks",
        lambda *a, **k: [check(cert_expiring_soon=True)],
    )

    body = client.get("/sites/1").get_data(as_text=True)

    assert "2026-12-01" in body
    assert "expiring soon" in body
    assert "badge-slow" in body


def test_a_site_without_a_certificate_shows_no_expiry(client, summaries, checks, monkeypatch):
    """A site without a certificate shows no expiry date."""
    monkeypatch.setattr(
        db,
        "get_recent_checks",
        lambda *a, **k: [check(cert_expires_at=None)],
    )

    body = client.get("/sites/1").get_data(as_text=True)

    assert "Certificate expires" not in body


# --------------------------------------------------------------------------
# The table of checks
# --------------------------------------------------------------------------

def test_the_table_shows_the_checks(client, summaries, checks):
    """The table shows the time, the result, the stage and the timing."""
    body = client.get("/sites/1").get_data(as_text=True)

    assert "2026-10-21 12:00:00 UTC" in body
    assert ">OK<" in body
    assert ">200<" in body
    assert ">100<" in body


def test_the_table_shows_the_failed_stage_of_a_failure(client, summaries, checks, monkeypatch):
    """A failed check shows the stage it failed in."""
    monkeypatch.setattr(
        db,
        "get_recent_checks",
        lambda *a, **k: [
            check(
                result="CONNECT_TIMEOUT",
                failed_stage="tcp",
                status_code=None,
                total_ms=None,
                error_detail="the connection timed out",
            )
        ],
    )

    body = client.get("/sites/1").get_data(as_text=True)

    assert ">CONNECT_TIMEOUT<" in body
    assert "tcp" in body
    assert "the connection timed out" in body


def test_the_table_shows_at_most_the_latest_20_checks(client, summaries, checks, monkeypatch):
    """Only the latest 20 checks are in the table."""
    rows = [
        check(id=number, checked_at=LATEST.replace(minute=number % 60))
        for number in range(25)
    ]
    monkeypatch.setattr(db, "get_recent_checks", lambda *a, **k: list(rows))

    body = client.get("/sites/1").get_data(as_text=True)

    assert body.count("<tr>") == 21  # 20 checks and the head row


def test_the_table_shows_no_detail_for_a_successful_check(client, summaries, checks):
    """A successful check has no error detail."""
    body = client.get("/sites/1").get_data(as_text=True)

    assert "error detail" not in body.lower()


# --------------------------------------------------------------------------
# An unknown site
# --------------------------------------------------------------------------

def test_an_unknown_site_answers_404(client, summaries, checks):
    """An id that is not a site answers with a 404."""
    response = client.get("/sites/999")

    assert response.status_code == 404
    assert "text/html" in response.headers["Content-Type"]


def test_the_404_page_is_a_page_of_the_dashboard(client, summaries, checks):
    """The 404 page has the same layout as the other pages."""
    body = client.get("/sites/999").get_data(as_text=True)

    assert "WhyDown" in body
    assert "Site not found" in body
    assert "<html" in body


def test_the_404_page_holds_no_chart(client, summaries, checks):
    """The 404 page loads no chart library."""
    body = client.get("/sites/999").get_data(as_text=True)

    assert CHART_CDN not in body
    assert "chart-data" not in body


def test_the_404_page_leaks_nothing(client, summaries, checks):
    """The 404 page holds no secret and no exception text."""
    body = client.get("/sites/999").get_data(as_text=True)

    assert SECRET_URL not in body
    assert "Traceback" not in body


# --------------------------------------------------------------------------
# A site without any check
# --------------------------------------------------------------------------

def test_a_site_without_checks_says_so(client, summaries, checks, monkeypatch):
    """A site without any check says "No checks yet"."""
    monkeypatch.setattr(db, "get_recent_checks", lambda *a, **k: [])

    response = client.get("/sites/1")

    assert response.status_code == 200
    assert "No checks yet" in response.get_data(as_text=True)


def test_a_site_without_checks_loads_no_chart_library(client, summaries, checks, monkeypatch):
    """Without any check the chart library is not loaded at all."""
    monkeypatch.setattr(db, "get_recent_checks", lambda *a, **k: [])

    body = client.get("/sites/1").get_data(as_text=True)

    assert CHART_CDN not in body
    assert "chart.js" not in body
    assert "chart-data" not in body


# --------------------------------------------------------------------------
# The charts
# --------------------------------------------------------------------------

def test_the_page_loads_the_pinned_chart_library(client, summaries, checks):
    """The chart library is loaded with one tag and a pinned version."""
    body = client.get("/sites/1").get_data(as_text=True)

    assert body.count(CHART_CDN) == 1
    assert "@latest" not in body


def test_the_page_has_no_inline_javascript(client, summaries, checks):
    """Every script of the page is either a file or the JSON element."""
    body = client.get("/sites/1").get_data(as_text=True)

    tags = script_tags(body)
    assert tags, "the page has no script at all"
    for attributes, content in tags:
        if "src=" in attributes:
            continue
        assert 'type="application/json"' in attributes
        json.loads(content)


def test_the_chart_data_holds_the_timings(client, summaries, checks):
    """The JSON element holds the timings of the checks."""
    body = client.get("/sites/1").get_data(as_text=True)
    data = chart_data(body)

    assert data["total_ms"] == [100]
    assert data["stages"]["dns_ms"] == [10]
    assert data["stages"]["tcp_ms"] == [20]
    assert data["stages"]["tls_ms"] == [30]
    assert data["stages"]["ttfb_ms"] == [40]


def test_the_chart_data_is_oldest_first(client, summaries, checks, monkeypatch):
    """The checks are ordered from the oldest to the newest."""
    monkeypatch.setattr(
        db,
        "get_recent_checks",
        lambda *a, **k: [
            check(id=3, checked_at=LATEST),
            check(id=2, checked_at=EARLIER),
            check(id=1, checked_at=OLDEST),
        ],
    )

    body = client.get("/sites/1").get_data(as_text=True)
    data = chart_data(body)

    assert data["labels"] == [
        "2026-10-21 11:40",
        "2026-10-21 11:50",
        "2026-10-21 12:00",
    ]


def test_a_missing_timing_is_a_gap_and_not_a_zero(client, summaries, checks, monkeypatch):
    """A timing that was never measured is null and never 0."""
    monkeypatch.setattr(
        db,
        "get_recent_checks",
        lambda *a, **k: [check(dns_ms=None, tls_ms=None, total_ms=None)],
    )

    body = client.get("/sites/1").get_data(as_text=True)
    data = chart_data(body)

    assert data["total_ms"] == [None]
    assert data["stages"]["dns_ms"] == [None]
    assert data["stages"]["tls_ms"] == [None]
    assert 0 not in data["stages"]["dns_ms"]
    assert 0 not in data["stages"]["tls_ms"]


def test_the_chart_data_holds_the_name_of_the_site(client, summaries, checks):
    """The JSON element holds the name of the site."""
    body = client.get("/sites/1").get_data(as_text=True)

    assert chart_data(body)["site"]["name"] == "Portal"


def test_the_chart_script_is_a_file_of_the_application(client, summaries, checks):
    """The chart code is a file of this application, not inline."""
    body = client.get("/sites/1").get_data(as_text=True)

    assert "/static/charts.js" in body


# --------------------------------------------------------------------------
# Chart containers and canvas attributes
# --------------------------------------------------------------------------

def test_each_chart_canvas_is_inside_a_chart_box(client, summaries, checks):
    """Every chart canvas is wrapped by an element with class chart-box."""
    body = client.get("/sites/1").get_data(as_text=True)

    parser = _CanvasAncestors()
    parser.feed(body)
    canvases = parser.results()
    assert len(canvases) == 2, "the page has two chart canvases"
    for ancestors, _attrs in canvases:
        # At least one ancestor has class chart-box: the box that sets the size.
        assert any("chart-box" in cls for cls in ancestors), (
            "a canvas is not inside a .chart-box container"
        )


def test_chart_canvas_has_no_width_and_height_attributes(client, summaries, checks):
    """A canvas has no width or height attribute: its box sets the size."""
    body = client.get("/sites/1").get_data(as_text=True)

    match = re.search(
        r'<canvas id="chart-total" class="chart-canvas"[^>]*>',
        body,
    )
    assert match, "the total canvas is missing"
    canvas_tag = match.group(0)
    assert "width=" not in canvas_tag
    assert "height=" not in canvas_tag

    match = re.search(
        r'<canvas id="chart-stages" class="chart-canvas"[^>]*>',
        body,
    )
    assert match, "the stages canvas is missing"
    canvas_tag = match.group(0)
    assert "width=" not in canvas_tag
    assert "height=" not in canvas_tag


def test_the_page_links_have_a_colour_variable(client, summaries, checks):
    """Links use the --link variable, not the default browser blue."""
    css = client.get("/static/style.css").get_data(as_text=True)

    assert "--link:" in css
    assert "var(--link)" in css


# --------------------------------------------------------------------------
# Escaping
# --------------------------------------------------------------------------

def test_a_site_name_with_markup_is_escaped_in_the_page(client, summaries, checks, monkeypatch):
    """A site name can never inject markup into the page."""
    monkeypatch.setattr(
        db,
        "site_summaries",
        lambda *a, **k: [summary(name="<script>alert(1)</script>")],
    )

    body = client.get("/sites/1").get_data(as_text=True)

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_a_site_name_with_markup_is_escaped_in_the_chart_data(client, summaries, checks, monkeypatch):
    """A site name can never end the JSON element of the charts."""
    monkeypatch.setattr(
        db,
        "site_summaries",
        lambda *a, **k: [summary(name="<script>alert(1)</script>")],
    )

    body = client.get("/sites/1").get_data(as_text=True)
    element_content = chart_element(body)

    assert "<script>alert(1)</script>" not in element_content
    assert "<" not in element_content
    assert chart_data(body)["site"]["name"] == "<script>alert(1)</script>"


def test_an_error_detail_with_markup_is_escaped_in_the_page(client, summaries, checks, monkeypatch):
    """An error message can never inject markup into the page."""
    monkeypatch.setattr(
        db,
        "get_recent_checks",
        lambda *a, **k: [
            check(
                result="DNS_FAILURE",
                error_detail="<script>alert(1)</script>",
            )
        ],
    )

    body = client.get("/sites/1").get_data(as_text=True)

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_an_error_detail_with_markup_is_escaped_in_the_chart_data(client, summaries, checks, monkeypatch):
    """An error message can never end the JSON element of the charts."""
    monkeypatch.setattr(
        db,
        "get_recent_checks",
        lambda *a, **k: [
            check(
                result="DNS_FAILURE",
                error_detail="<script>alert(1)</script>",
            )
        ],
    )

    body = client.get("/sites/1").get_data(as_text=True)
    element_content = chart_element(body)

    assert "<script>alert(1)</script>" not in element_content
    assert "<" not in element_content
    assert chart_data(body)["checks"][0]["error_detail"] == "<script>alert(1)</script>"


# --------------------------------------------------------------------------
# The database
# --------------------------------------------------------------------------

def test_a_request_uses_one_connection(client, summaries, checks, connection):
    """The page opens one connection and uses it for both queries."""
    client.get("/sites/1")

    assert len(connection.opened) == 1


def test_a_request_closes_its_connection(client, summaries, checks, connection):
    """The connection of a request is closed again."""
    client.get("/sites/1")

    assert connection.closed is True


def test_a_failed_request_closes_its_connection(client, monkeypatch, connection):
    """A failing request closes its connection as well."""
    def boom(*args, **kwargs):
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "site_summaries", boom)

    client.get("/sites/1")

    assert connection.closed is True


def test_a_database_failure_answers_503(client, monkeypatch):
    """A failing database is answered with a 503 page."""
    def boom(*args, **kwargs):
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "site_summaries", boom)

    response = client.get("/sites/1")

    assert response.status_code == 503
    assert "text/html" in response.headers["Content-Type"]


def test_a_failing_check_query_answers_503(client, summaries, monkeypatch):
    """A failing history query is answered with a 503 as well."""
    def boom(*args, **kwargs):
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "get_recent_checks", boom)

    response = client.get("/sites/1")

    assert response.status_code == 503


def test_the_503_page_holds_a_fixed_message(client, monkeypatch):
    """The 503 page says that the dashboard is not available."""
    def boom(*args, **kwargs):
        raise db.DatabaseError("the database is gone")

    monkeypatch.setattr(db, "site_summaries", boom)

    body = client.get("/sites/1").get_data(as_text=True)

    assert "not available" in body.lower()
    assert "the database is gone" not in body


def test_a_database_failure_leaks_nothing(client, monkeypatch, caplog):
    """The 503 page and the log hold neither a traceback nor the DSN."""
    def boom(*args, **kwargs):
        raise db.DatabaseError(f"could not connect to {SECRET_URL}")

    monkeypatch.setattr(db, "site_summaries", boom)

    with caplog.at_level(logging.DEBUG):
        response = client.get("/sites/1")

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

def test_no_secret_appears_in_the_page(client, summaries, checks, monkeypatch):
    """Neither the token nor the database URL ends up in the page."""
    monkeypatch.setenv("CRON_SECRET", SECRET_TOKEN)
    monkeypatch.setenv("DATABASE_URL", SECRET_URL)

    response = client.get("/sites/1")

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

    body = client.get("/sites/1").get_data(as_text=True)

    assert SECRET_URL not in body
    assert "sup3rs3cret" not in body
