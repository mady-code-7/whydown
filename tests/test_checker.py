"""Tests for network checking functions."""

import socket
import time

import pytest

from app.checker import (
    check_url,
    connect_tcp,
    do_http,
    resolve_dns,
)
from app.classifier import classify_result


class _FakeSocket:
    """Minimal socket stand-in that records whether it was closed."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class TestDNSResolution:
    """Tests for DNS resolution."""

    def test_successful_dns_resolution(self, monkeypatch):
        """Test successful DNS resolution (resolver is mocked, no network)."""

        def mock_getaddrinfo(*args, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            ]

        monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)

        success, ip, elapsed_ms, error = resolve_dns("monitored.test", timeout=5.0)

        assert success is True
        assert ip == "93.184.216.34"
        assert elapsed_ms is not None
        assert elapsed_ms >= 0
        assert error is None

    def test_dns_timeout(self, monkeypatch):
        """Test DNS resolution timeout."""
        import socket

        def mock_getaddrinfo(*args, **kwargs):
            raise TimeoutError("DNS resolution timeout")

        monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)

        success, ip, elapsed_ms, error = resolve_dns("nonexistentdomain.invalid", timeout=5.0)

        assert success is False
        assert ip is None
        assert elapsed_ms is None
        assert "timeout" in error.lower()

    def test_dns_failure(self, monkeypatch):
        """Test DNS resolution failure."""
        import socket

        def mock_getaddrinfo(*args, **kwargs):
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)

        success, ip, elapsed_ms, error = resolve_dns("invalid-domain-12345.invalid", timeout=5.0)

        assert success is False
        assert ip is None
        assert elapsed_ms is None
        assert error is not None


class TestTCPConnection:
    """Tests for TCP connection."""

    @pytest.fixture
    def real_server(self):
        """Create a simple TCP server for testing."""
        import socket
        import threading
        import time

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', 0))
        port = server.getsockname()[1]
        server.listen(5)

        received = []

        def handle_client(client_sock):
            try:
                data = client_sock.recv(1024)
                received.append(data)
                client_sock.send(b"ACK")
            finally:
                client_sock.close()

        def accept_loop():
            while True:
                try:
                    client_sock, _ = server.accept()
                    t = threading.Thread(target=handle_client, args=(client_sock,))
                    t.daemon = True
                    t.start()
                except OSError:
                    break

        thread = threading.Thread(target=accept_loop)
        thread.daemon = True
        thread.start()

        time.sleep(0.1)  # Give time for server to be ready

        yield port, received, server

        server.close()

    def test_successful_tcp_connection(self, real_server):
        """Test successful TCP connection to a real server."""
        port, _received, _server = real_server

        success, sock, elapsed_ms, error = connect_tcp("127.0.0.1", str(port), timeout=5.0)

        assert success is True
        assert sock is not None
        assert elapsed_ms is not None
        assert error is None
        # Connection succeeded; close the returned socket
        sock.close()

    def test_tcp_connection_timeout(self, monkeypatch):
        """Test TCP connection timeout."""
        import socket

        def mock_connect(*args, **kwargs):
            raise TimeoutError("Connection timeout")

        monkeypatch.setattr(socket.socket, "connect", mock_connect)

        success, _sock, elapsed_ms, error = connect_tcp("192.0.2.1", "80", timeout=5.0)

        assert success is False
        assert _sock is None
        assert elapsed_ms is None
        assert "timeout" in error.lower()

    def test_tcp_connection_refused(self, monkeypatch):
        """Test TCP connection refused."""
        import socket

        def mock_connect(*args, **kwargs):
            raise ConnectionRefusedError("Connection refused")

        monkeypatch.setattr(socket.socket, "connect", mock_connect)

        success, _sock, elapsed_ms, error = connect_tcp("127.0.0.1", "9999", timeout=5.0)

        assert success is False
        assert _sock is None
        assert elapsed_ms is None
        assert "refused" in error.lower()


class TestClassifier:
    """Tests for failure classification."""

    def test_classify_ok(self):
        """Test classification of OK result."""
        result, stage = classify_result(
            dns_success=True,
            tcp_success=True,
            tls_success=True,
            http_success=True,
            status_code=200,
            ttfb_ms=100.0,
            total_ms=200.0,
            slow_threshold_ms=5000,
            keyword_found=True,
        )

        assert result == "OK"
        assert stage is None

    def test_classify_slow(self):
        """Test classification of slow response."""
        result, stage = classify_result(
            dns_success=True,
            tcp_success=True,
            tls_success=True,
            http_success=True,
            status_code=200,
            ttfb_ms=100.0,
            total_ms=6000.0,  # Slower than threshold
            slow_threshold_ms=5000,
            keyword_found=True,
        )

        assert result == "SLOW"
        assert stage is None

    def test_classify_tcp_timeout(self):
        """Test that a TCP timeout yields CONNECT_TIMEOUT, not CONNECTION_REFUSED."""
        result, stage = classify_result(
            dns_success=True,
            tcp_success=False,
            tls_success=False,
            http_success=False,
            status_code=None,
            ttfb_ms=None,
            total_ms=None,
            slow_threshold_ms=5000,
            tcp_error="Connection timeout",
        )

        assert result == "CONNECT_TIMEOUT"
        assert stage == "tcp"

    def test_classify_3xx_matches_expected(self):
        """Test 3xx status matching expected_status continues as OK."""
        result, stage = classify_result(
            dns_success=True,
            tcp_success=True,
            tls_success=True,
            http_success=True,
            status_code=301,
            ttfb_ms=100.0,
            total_ms=200.0,
            slow_threshold_ms=5000,
            keyword_found=None,
            expected_status=301,
        )

        assert result == "OK"
        assert stage is None

    def test_classify_3xx_not_expected(self):
        """Test 3xx status not matching expected_status yields HTTP_3XX."""
        result, stage = classify_result(
            dns_success=True,
            tcp_success=True,
            tls_success=True,
            http_success=True,
            status_code=301,
            ttfb_ms=100.0,
            total_ms=200.0,
            slow_threshold_ms=5000,
            keyword_found=None,
            expected_status=200,
        )

        assert result == "HTTP_3XX"
        assert stage == "http"

    def test_classify_dns_failure(self):
        """Test classification of DNS failure."""
        result, stage = classify_result(
            dns_success=False,
            tcp_success=False,
            tls_success=False,
            http_success=False,
            status_code=None,
            ttfb_ms=None,
            total_ms=None,
            slow_threshold_ms=5000,
            keyword_found=None,
        )

        assert result == "DNS_FAILURE"
        assert stage == "dns"

    def test_classify_tcp_failure(self):
        """Test classification of TCP failure."""
        result, stage = classify_result(
            dns_success=True,
            tcp_success=False,
            tls_success=False,
            http_success=False,
            status_code=None,
            ttfb_ms=None,
            total_ms=None,
            slow_threshold_ms=5000,
            keyword_found=None,
        )

        assert result == "CONNECTION_REFUSED"
        assert stage == "tcp"

    def test_classify_tls_failure(self):
        """Test classification of TLS failure."""
        result, stage = classify_result(
            dns_success=True,
            tcp_success=True,
            tls_success=False,
            http_success=False,
            status_code=None,
            ttfb_ms=None,
            total_ms=None,
            slow_threshold_ms=5000,
            keyword_found=None,
        )

        assert result == "TLS_ERROR"
        assert stage == "tls"

    def test_classify_http_4xx(self):
        """Test classification of HTTP 4xx error."""
        result, stage = classify_result(
            dns_success=True,
            tcp_success=True,
            tls_success=True,
            http_success=True,
            status_code=404,
            ttfb_ms=100.0,
            total_ms=200.0,
            slow_threshold_ms=5000,
            keyword_found=None,
        )

        assert result == "HTTP_4XX"
        assert stage == "http"

    def test_classify_http_5xx(self):
        """Test classification of HTTP 5xx error."""
        result, stage = classify_result(
            dns_success=True,
            tcp_success=True,
            tls_success=True,
            http_success=True,
            status_code=500,
            ttfb_ms=100.0,
            total_ms=200.0,
            slow_threshold_ms=5000,
            keyword_found=None,
        )

        assert result == "HTTP_5XX"
        assert stage == "http"

    def test_classify_keyword_missing(self):
        """Test classification of missing keyword."""
        result, stage = classify_result(
            dns_success=True,
            tcp_success=True,
            tls_success=True,
            http_success=True,
            status_code=200,
            ttfb_ms=100.0,
            total_ms=200.0,
            slow_threshold_ms=5000,
            keyword_found=False,
        )

        assert result == "KEYWORD_MISSING"
        assert stage == "http"

    def test_classify_dns_slow(self):
        """A slow resolver gets its own code."""
        result, stage = classify_result(
            dns_success=True,
            tcp_success=True,
            tls_success=True,
            http_success=True,
            status_code=200,
            ttfb_ms=100.0,
            total_ms=6100.0,
            slow_threshold_ms=5000,
            dns_ms=6000.0,
        )

        assert result == "DNS_SLOW"
        # Nothing failed, so no stage is marked as failed.
        assert stage is None

    def test_classify_failure_wins_over_dns_slow(self):
        """A hard failure is never masked by a slow resolver."""
        result, stage = classify_result(
            dns_success=True,
            tcp_success=False,
            tls_success=False,
            http_success=False,
            status_code=None,
            ttfb_ms=None,
            total_ms=None,
            slow_threshold_ms=5000,
            dns_ms=6000.0,
        )

        assert result == "CONNECTION_REFUSED"
        assert stage == "tcp"


class TestCheckUrlIntegration:
    """Integration tests for complete URL checking."""

    def test_check_http_url(self, monkeypatch):
        """Test checking an HTTP URL (all network stages are mocked)."""
        monkeypatch.setattr(
            "app.checker.resolve_dns",
            lambda hostname, timeout=5.0: (True, "93.184.216.34", 1.0, None),
        )
        monkeypatch.setattr(
            "app.checker.connect_tcp",
            lambda ip, port, timeout=8.0: (True, _FakeSocket(), 2.0, None),
        )
        monkeypatch.setattr(
            "app.checker.do_http",
            lambda *args, **kwargs: (True, 200, 5, 20.0, 25.0, "hello", None),
        )

        result = check_url(
            "http://monitored.test",
            slow_threshold_ms=5000,
            cert_expiry_warning_days=7,
        )

        assert result is not None
        assert result.dns_success is True
        assert result.tcp_success is True
        assert result.http_success is True
        assert result.status_code == 200
        assert result.resolved_ip == "93.184.216.34"
        assert result.total_ms is not None
        assert result.result == "OK"

    def test_check_https_url(self, monkeypatch):
        """Test checking an HTTPS URL (all network stages are mocked)."""
        monkeypatch.setattr(
            "app.checker.resolve_dns",
            lambda hostname, timeout=5.0: (True, "93.184.216.34", 1.0, None),
        )
        monkeypatch.setattr(
            "app.checker.connect_tcp",
            lambda ip, port, timeout=8.0: (True, _FakeSocket(), 2.0, None),
        )
        monkeypatch.setattr(
            "app.checker.do_tls",
            lambda *args, **kwargs: (
                True, _FakeSocket(), 3.0, None, {"subject": {}}, None, False, False, False,
            ),
        )
        monkeypatch.setattr(
            "app.checker.do_http",
            lambda *args, **kwargs: (True, 200, 5, 20.0, 25.0, "hello", None),
        )

        result = check_url(
            "https://monitored.test",
            slow_threshold_ms=5000,
            cert_expiry_warning_days=7,
        )

        assert result is not None
        assert result.dns_success is True
        assert result.tcp_success is True
        assert result.tls_success is True
        assert result.http_success is True
        assert result.status_code == 200
        assert result.tls_ms == 3.0
        assert result.total_ms is not None
        assert result.result == "OK"

    def test_check_invalid_url(self):
        """Test checking an invalid URL."""
        result = check_url(
            "not-a-valid-url",
            slow_threshold_ms=5000,
            cert_expiry_warning_days=7,
        )

        assert result is not None
        assert result.dns_success is False
        assert result.failed_stage == "url_parse"
        assert result.error_message is not None
        assert result.result != "OK"

    def test_check_private_ip_blocked(self, monkeypatch):
        """Test that private IP addresses are blocked (SSRF -> BLOCKED_TARGET)."""

        def mock_resolve_dns(hostname, timeout=5.0):
            return True, "192.168.1.1", 10.0, None

        monkeypatch.setattr("app.checker.resolve_dns", mock_resolve_dns)

        result = check_url(
            "http://monitored.test",  # Actual hostname
            slow_threshold_ms=5000,
            cert_expiry_warning_days=7,
        )

        assert result is not None
        # Since we mocked DNS to resolve to a private IP, it should be blocked
        assert result.result == "BLOCKED_TARGET"
        assert result.failed_stage == "dns"


class TestCheckUrlFailureClassification:
    """Failures observed through check_url must be classified precisely.

    The classifier can tell a connect timeout from a refused connection only
    when the TCP stage's error message is passed on, so these tests cover the
    wiring between the stage functions and the classifier.
    """

    @staticmethod
    def _mock_dns(monkeypatch, ip="93.184.216.34"):
        monkeypatch.setattr(
            "app.checker.resolve_dns",
            lambda hostname, timeout=5.0: (True, ip, 1.0, None),
        )

    def test_connect_timeout_is_classified_as_connect_timeout(self, monkeypatch):
        """A TCP connect that times out must not be reported as refused."""
        import socket

        self._mock_dns(monkeypatch)

        def mock_connect(self, address):
            raise TimeoutError("timed out")

        monkeypatch.setattr(socket.socket, "connect", mock_connect)

        result = check_url("http://monitored.test")

        assert result.result == "CONNECT_TIMEOUT"
        assert result.failed_stage == "tcp"

    def test_connection_refused_is_classified_as_refused(self, monkeypatch):
        """A TCP connect that is actively refused must be CONNECTION_REFUSED."""
        import socket

        self._mock_dns(monkeypatch)

        def mock_connect(self, address):
            raise ConnectionRefusedError("Connection refused")

        monkeypatch.setattr(socket.socket, "connect", mock_connect)

        result = check_url("http://monitored.test")

        assert result.result == "CONNECTION_REFUSED"
        assert result.failed_stage == "tcp"

    def test_dns_failure_is_classified_as_dns_failure(self, monkeypatch):
        """A hostname that does not resolve must fail at the dns stage."""
        import socket

        def mock_getaddrinfo(*args, **kwargs):
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)

        result = check_url("http://does-not-exist.invalid")

        assert result.result == "DNS_FAILURE"
        assert result.failed_stage == "dns"
        assert result.error_message is not None


class TestDoHttp:
    """Tests for the HTTP stage, driven against a local socket."""

    @pytest.fixture
    def local_server(self):
        """Factory starting a one-shot TCP server with a custom handler."""
        import threading

        started = []

        def start(handler):
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            port = server.getsockname()[1]
            server.listen(1)

            def serve():
                try:
                    client, _ = server.accept()
                except OSError:
                    return
                try:
                    handler(client)
                finally:
                    client.close()

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            started.append((server, thread))
            return port

        yield start

        for server, thread in started:
            server.close()
            thread.join(timeout=2)

    def test_ttfb_is_the_time_to_first_byte(self, local_server):
        """TTFB must not include the time spent reading the body."""
        def handler(client):
            client.recv(4096)
            client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n")
            time.sleep(0.3)
            client.sendall(b"hello")

        port = local_server(handler)
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)

        success, status_code, size, ttfb_ms, total_ms, body, error = do_http(
            sock, "/", "127.0.0.1", timeout=5
        )

        assert success is True
        assert error is None
        assert status_code == 200
        assert body == "hello"
        assert size == 5
        # The body arrived 0.3s later, so the first byte must be much earlier.
        assert total_ms >= 300
        assert ttfb_ms < total_ms - 100

    def test_read_timeout_when_server_never_replies(self, local_server):
        """A server that accepts but never answers is a read timeout."""
        port = local_server(lambda client: time.sleep(2))
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)

        success, _status_code, _size, _ttfb, _total, body, error = do_http(
            sock, "/", "127.0.0.1", timeout=0.5
        )

        assert success is False
        assert error == "HTTP read timeout"
        assert body is None

    def test_dropped_connection_is_reported(self, local_server):
        """A connection closed before any byte arrives is not a success."""
        port = local_server(lambda client: None)
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)

        success, _status, _size, _ttfb, _total, body, error = do_http(
            sock, "/", "127.0.0.1", timeout=5
        )

        assert success is False
        assert error is not None
        assert body is None


class TestCheckUrlSafetyNet:
    """A check must never raise and must never leak a socket."""

    @staticmethod
    def _stub_dns(monkeypatch, ip="93.184.216.34"):
        monkeypatch.setattr(
            "app.checker.resolve_dns",
            lambda hostname, timeout=5.0: (True, ip, 1.0, None),
        )

    def test_unexpected_error_is_reported_as_internal_error(self, monkeypatch):
        """An unexpected exception is classified, not raised."""
        def boom(url):
            raise RuntimeError("kaboom")

        monkeypatch.setattr("app.checker.parse_url", boom)

        result = check_url("http://monitored.test")

        assert result.result == "INTERNAL_ERROR"
        assert result.failed_stage == "unknown"
        assert "kaboom" in result.error_message
        assert result.total_ms is not None

    def test_tls_failure_closes_the_tcp_socket(self, monkeypatch):
        """A failed handshake must not leave the TCP socket open."""
        sock = _FakeSocket()
        self._stub_dns(monkeypatch)
        monkeypatch.setattr(
            "app.checker.connect_tcp",
            lambda ip, port, timeout=8.0: (True, sock, 1.0, None),
        )
        monkeypatch.setattr(
            "app.checker.do_tls",
            lambda *args, **kwargs: (
                False, None, 1.0, "handshake failed", None, None, False, False, False,
            ),
        )

        result = check_url("https://monitored.test")

        assert result.result == "TLS_ERROR"
        assert result.failed_stage == "tls"
        assert sock.closed is True

    def test_http_failure_closes_the_socket(self, monkeypatch):
        """A failed HTTP stage must not leave the socket open."""
        sock = _FakeSocket()
        self._stub_dns(monkeypatch)
        monkeypatch.setattr(
            "app.checker.connect_tcp",
            lambda ip, port, timeout=8.0: (True, sock, 1.0, None),
        )
        monkeypatch.setattr(
            "app.checker.do_http",
            lambda *args, **kwargs: (
                False, None, None, None, None, None, "connection dropped",
            ),
        )

        result = check_url("http://monitored.test")

        assert result.result == "READ_TIMEOUT"
        assert result.failed_stage == "http"
        assert sock.closed is True


class TestCheckUrlTiming:
    """total_ms must cover the whole check, not just the HTTP stage."""

    @staticmethod
    def _stub_stages(monkeypatch, dns_ms=1.0, http_total_ms=25.0):
        monkeypatch.setattr(
            "app.checker.resolve_dns",
            lambda hostname, timeout=5.0: (True, "93.184.216.34", dns_ms, None),
        )
        monkeypatch.setattr(
            "app.checker.connect_tcp",
            lambda ip, port, timeout=8.0: (True, _FakeSocket(), 5.0, None),
        )
        monkeypatch.setattr(
            "app.checker.do_http",
            lambda *args, **kwargs: (
                True, 200, 5, 20.0, http_total_ms, "body", None,
            ),
        )

    def test_total_ms_covers_the_dns_stage(self, monkeypatch):
        """The reported total includes the time spent before the request."""
        self._stub_stages(monkeypatch)

        def slow_dns(hostname, timeout=5.0):
            time.sleep(0.1)
            return True, "93.184.216.34", 100.0, None

        monkeypatch.setattr("app.checker.resolve_dns", slow_dns)

        result = check_url("http://monitored.test", slow_threshold_ms=5000)

        assert result.dns_ms == 100.0
        assert result.total_ms >= result.dns_ms
        assert result.total_ms >= result.ttfb_ms
        assert result.result == "OK"

    def test_slow_dns_is_classified_as_dns_slow(self, monkeypatch):
        """A slow resolver gets its own code instead of the generic SLOW."""
        self._stub_stages(monkeypatch, dns_ms=6000.0)

        result = check_url("http://monitored.test", slow_threshold_ms=5000)

        assert result.result == "DNS_SLOW"
        # Nothing failed, so no stage is marked as failed.
        assert result.failed_stage is None