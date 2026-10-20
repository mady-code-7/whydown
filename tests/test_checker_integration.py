"""Integration tests: the real checker against the local mock server.

These tests drive the real ``check_url()`` over real sockets towards the
mock server from ``tests/mock_server.py`` (spec sections 13.2 and 13.3). The
mock server listens on 127.0.0.1, so the checker needs its test-only
``allow_private`` switch - the SSRF filter blocks loopback addresses by
default, and one test proves that it still does.

No test in this module touches the public internet: the only hostname used
is an IP literal, and the DNS failure is produced by stubbing the resolver
with the NXDOMAIN that a real resolver returns for a ``.invalid`` name.
"""

from __future__ import annotations

import inspect
import socket

import pytest
from mock_server import MARKER

from app.checker import NetworkTimeouts, check_url

# Short timeouts: the whole module has to stay well below the 15 second
# budget of the test suite.
FAST_TIMEOUTS = NetworkTimeouts(
    dns_timeout=1.0,
    connect_timeout=1.0,
    tls_timeout=1.0,
    read_timeout=1.0,
    total_timeout=5.0,
)

# The mock server never answers /hang, so the read timeout has to be short.
HANG_TIMEOUTS = NetworkTimeouts(
    dns_timeout=1.0,
    connect_timeout=1.0,
    tls_timeout=1.0,
    read_timeout=0.5,
    total_timeout=5.0,
)


def _check(url: str, **kwargs):
    """Run a check against the mock server with test-friendly defaults."""
    kwargs.setdefault("timeouts", FAST_TIMEOUTS)
    # The mock server lives on the loopback interface, which the SSRF filter
    # blocks unless the test explicitly opts in.
    kwargs.setdefault("allow_private", True)
    return check_url(url, **kwargs)


def _closed_port() -> int:
    """Return a port on 127.0.0.1 that nothing is listening on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestHealthySite:
    """T01: a healthy site is OK and every stage is timed."""

    def test_healthy_site_reports_ok(self, mock_server):
        result = _check(mock_server.url("/ok"), slow_threshold_ms=5000)

        assert result.result == "OK"
        assert result.failed_stage is None
        assert result.status_code == 200
        assert result.dns_success is True
        assert result.tcp_success is True
        assert result.http_success is True
        assert result.resolved_ip == "127.0.0.1"

    def test_all_stage_times_are_recorded(self, mock_server):
        result = _check(mock_server.url("/ok"), slow_threshold_ms=5000)

        assert result.dns_ms is not None and result.dns_ms >= 0
        assert result.tcp_ms is not None and result.tcp_ms >= 0
        assert result.ttfb_ms is not None and result.ttfb_ms >= 0
        assert result.total_ms is not None and result.total_ms >= 0
        # The total covers the whole check, not just the HTTP stage.
        assert result.total_ms >= result.ttfb_ms

    def test_healthy_site_with_matching_keyword(self, mock_server):
        """The marker is present in a healthy body."""
        result = _check(
            mock_server.url("/ok"),
            slow_threshold_ms=5000,
            expected_keyword=MARKER,
        )

        assert result.result == "OK"
        assert result.keyword_found is True


class TestDnsFailure:
    """T02: a name that does not resolve fails at the dns stage."""

    def test_non_existent_domain_is_dns_failure(self, monkeypatch):
        """A .invalid domain (RFC 2606) never resolves.

        The resolver is stubbed with the NXDOMAIN a real resolver returns for
        such a name, so the test stays offline while still exercising the
        real DNS stage of the checker.
        """

        def fake_getaddrinfo(host, port, *args, **kwargs):
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

        result = _check("http://does-not-exist.invalid")

        assert result.result == "DNS_FAILURE"
        assert result.failed_stage == "dns"
        assert result.error_message is not None
        assert result.status_code is None


class TestTcpFailure:
    """T03: a closed port fails at the TCP stage."""

    def test_closed_port_fails_at_the_tcp_stage(self):
        """A port nobody listens on fails at the TCP stage.

        Which code comes out depends on the operating system: Linux answers
        the SYN with a RST (-> CONNECTION_REFUSED), while the Windows firewall
        on this machine silently drops it (-> CONNECT_TIMEOUT). Both are TCP
        failures - the point of the test is that the failure is localised to
        the TCP stage and never reported as an HTTP error. The mapping of a
        real refusal to CONNECTION_REFUSED is covered by the unit tests in
        tests/test_checker.py.
        """
        port = _closed_port()

        result = _check(f"http://127.0.0.1:{port}/ok")

        assert result.result in {"CONNECTION_REFUSED", "CONNECT_TIMEOUT"}
        assert result.failed_stage == "tcp"
        assert result.dns_success is True
        assert result.tcp_success is False
        assert result.error_message is not None
        assert result.status_code is None


class TestHang:
    """T04: a server that accepts but never answers is a read timeout."""

    def test_hanging_server_is_read_timeout(self, mock_server):
        result = _check(mock_server.url("/hang"), timeouts=HANG_TIMEOUTS)

        assert result.result == "READ_TIMEOUT"
        assert result.failed_stage == "http"
        assert result.status_code is None
        # A hang is not an HTTP error status.
        assert result.result != "HTTP_5XX"


class TestSlowResponse:
    """T05: a response slower than the threshold is SLOW."""

    def test_slow_response_is_classified_slow(self, mock_server):
        """The mock server delays its answer, which exceeds the threshold.

        The checker sends the path of the URL without its query string, so
        the mock server applies its default delay of 250 ms here.
        """
        result = _check(mock_server.url("/slow"), slow_threshold_ms=100)

        assert result.result == "SLOW"
        assert result.failed_stage is None
        assert result.status_code == 200
        # The delay really happened.
        assert result.total_ms >= 250

    def test_same_response_is_ok_with_a_generous_threshold(self, mock_server):
        """The same endpoint is healthy when the threshold is high enough."""
        result = _check(mock_server.url("/slow"), slow_threshold_ms=5000)

        assert result.result == "OK"


class TestHttpErrorStatus:
    """T06: an unexpected status code is classified by its class."""

    def test_http_500_is_server_error(self, mock_server):
        result = _check(mock_server.url("/status/500"))

        assert result.result == "HTTP_5XX"
        assert result.failed_stage == "http"
        assert result.status_code == 500

    def test_http_404_is_client_error(self, mock_server):
        result = _check(mock_server.url("/status/404"))

        assert result.result == "HTTP_4XX"
        assert result.failed_stage == "http"
        assert result.status_code == 404

    @pytest.mark.xfail(
        reason=(
            "classify_result() checks the 4xx/5xx range before it compares the "
            "status with expected_status, so a site that is expected to answer "
            "404 is still reported as HTTP_4XX. Spec section 8.3 calls "
            "HTTP_4XX an 'unexpected' 4xx/5xx, so the classifier has to "
            "honour expected_status here."
        ),
        strict=True,
    )
    def test_expected_status_is_not_an_error(self, mock_server):
        """A 404 that the user expects is fine."""
        result = _check(mock_server.url("/status/404"), expected_status=404)

        assert result.result == "OK"
        assert result.status_code == 404


class TestKeywordMissing:
    """T20: a page that loads but lacks the expected text."""

    def test_keyword_missing_is_reported(self, mock_server):
        result = _check(
            mock_server.url("/keyword-missing"),
            expected_keyword=MARKER,
        )

        assert result.result == "KEYWORD_MISSING"
        assert result.failed_stage == "http"
        assert result.status_code == 200
        assert result.keyword_found is False

    def test_keyword_is_not_checked_without_an_expected_keyword(self, mock_server):
        result = _check(mock_server.url("/keyword-missing"))

        assert result.result == "OK"
        assert result.keyword_found is None


class TestDroppedConnection:
    """A connection closed before any byte arrives is not an HTTP error."""

    def test_dropped_connection_is_not_reported_as_http_5xx(self, mock_server):
        result = _check(mock_server.url("/drop"))

        assert result.result != "HTTP_5XX"
        assert result.result == "READ_TIMEOUT"
        assert result.failed_stage == "http"
        assert result.status_code is None


class TestMockDownAndUp:
    """The mock site can be switched down and up again."""

    def test_site_reports_down_while_switched_off(self, mock_server):
        mock_server.set_down()
        assert mock_server.is_down is True

        result = _check(mock_server.url("/ok"))

        assert result.result == "HTTP_5XX"
        assert result.status_code == 503

    def test_site_is_healthy_again_after_switching_on(self, mock_server):
        mock_server.set_down()
        mock_server.set_up()
        assert mock_server.is_down is False

        result = _check(mock_server.url("/ok"))

        assert result.result == "OK"
        assert result.status_code == 200


class TestSsrfFilter:
    """T15: private and loopback addresses are blocked by default."""

    def test_allow_private_is_off_by_default(self):
        parameter = inspect.signature(check_url).parameters["allow_private"]
        assert parameter.default is False

    def test_loopback_is_blocked_without_the_test_flag(self, mock_server):
        result = check_url(mock_server.url("/ok"), timeouts=FAST_TIMEOUTS)

        assert result.result == "BLOCKED_TARGET"
        assert result.failed_stage == "dns"
        assert result.error_message is not None
        assert "blocked" in result.error_message.lower()

    def test_private_range_is_blocked_without_the_test_flag(self, monkeypatch):
        def mock_resolve_dns(hostname, timeout=5.0):
            return True, "192.168.1.1", 1.0, None

        monkeypatch.setattr("app.checker.resolve_dns", mock_resolve_dns)

        result = check_url("http://monitored.test", timeouts=FAST_TIMEOUTS)

        assert result.result == "BLOCKED_TARGET"
        assert result.failed_stage == "dns"

    def test_loopback_is_reachable_with_the_test_flag(self, mock_server):
        """The same target is reachable once the test opts in."""
        result = _check(mock_server.url("/ok"))

        assert result.result == "OK"
        assert result.status_code == 200


@pytest.mark.parametrize("path", ["/ok", "/status/500", "/keyword-missing"])
def test_repeated_checks_do_not_break_the_server(mock_server, path):
    """The mock server survives a series of checks (one connection each)."""
    for _ in range(3):
        result = _check(mock_server.url(path))
        assert result.result != "INTERNAL_ERROR"
        assert result.total_ms is not None
