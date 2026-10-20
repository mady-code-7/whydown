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
import threading
import time
from contextlib import suppress

import pytest
from mock_server import MARKER, MockServer

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


class SilentTlsServer:
    """A TCP server that accepts connections but never speaks TLS.

    The client sends its ClientHello and waits for a ServerHello that never
    arrives, so the handshake can only end in a timeout. This is the
    situation of a server that accepts the connection but is stuck before
    the TLS negotiation.
    """

    def __init__(self, host: str = "127.0.0.1") -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, 0))
        self._sock.listen(8)
        self._stop = threading.Event()
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def url(self) -> str:
        """HTTPS URL of this server."""
        return f"https://127.0.0.1:{self.port}/"

    def stop(self) -> None:
        """Stop the server and release the port."""
        self._stop.set()
        with suppress(OSError):
            self._sock.close()
        self._thread.join(timeout=2)

    def _accept_loop(self) -> None:
        """Accept connections and keep them open without answering."""
        self._sock.settimeout(0.05)
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                # Only means "no client yet".
                continue
            except OSError:
                # The listening socket was closed by stop().
                break
            threading.Thread(target=self._hold, args=(conn,), daemon=True).start()

    @staticmethod
    def _hold(conn: socket.socket) -> None:
        """Read whatever arrives and never answer."""
        try:
            while conn.recv(4096):
                pass
        except OSError:
            # The client gave up and closed the connection.
            pass
        finally:
            with suppress(OSError):
                conn.close()


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

        The URL has no query string, so the mock server applies its default
        delay of 250 ms here.
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

    def test_expected_status_is_not_an_error(self, mock_server):
        """A 404 that the user expects is fine."""
        result = _check(mock_server.url("/status/404"), expected_status=404)

        assert result.result == "OK"
        assert result.status_code == 404

    def test_expected_500_is_not_an_error(self, mock_server):
        """A 500 that the user expects is fine."""
        result = _check(mock_server.url("/status/500"), expected_status=500)

        assert result.result == "OK"
        assert result.status_code == 500


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


class TestTlsTimeout:
    """The TLS stage is bounded by the configured tls_timeout."""

    def test_handshake_that_never_completes_fails_at_the_tls_stage(self):
        """A server that never finishes the handshake times out.

        The timeout of the TLS stage is the one from NetworkTimeouts, so the
        check gives up after about half a second instead of waiting for the
        timeout of the connect stage (5 s here).
        """
        timeouts = NetworkTimeouts(
            dns_timeout=1.0,
            connect_timeout=5.0,
            tls_timeout=0.5,
            read_timeout=1.0,
            total_timeout=10.0,
        )
        server = SilentTlsServer()
        try:
            start = time.perf_counter()
            result = check_url(
                server.url(), timeouts=timeouts, allow_private=True
            )
            elapsed = time.perf_counter() - start
        finally:
            server.stop()

        assert result.result == "TLS_ERROR"
        assert result.failed_stage == "tls"
        assert result.tls_success is False
        assert result.status_code is None
        # The handshake gave up after about the TLS timeout.
        assert elapsed < 2.0


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


class TestQueryString:
    """The query string of the monitored URL is forwarded to the server."""

    def test_query_string_delay_is_applied(self, mock_server):
        """The mock server applies the delay asked for in the query string."""
        result = _check(mock_server.url("/slow?ms=250"), slow_threshold_ms=100)

        assert result.result == "SLOW"
        assert result.failed_stage is None
        assert result.status_code == 200
        # The delay really happened.
        assert result.total_ms >= 250

    def test_query_string_reaches_the_server(self):
        """A delay that differs from the server default proves forwarding.

        The server is started with a default delay of 0 ms, so only the
        "?ms=250" of the URL can produce the observed wait.
        """
        server = MockServer(slow_ms=0)
        try:
            result = check_url(
                server.url("/slow?ms=250"),
                slow_threshold_ms=100,
                timeouts=FAST_TIMEOUTS,
                allow_private=True,
            )

            assert result.result == "SLOW"
            assert result.total_ms >= 250
        finally:
            server.stop()
