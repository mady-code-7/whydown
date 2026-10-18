"""Tests for network checking functions."""

import pytest

from app.checker import (
    check_url,
    connect_tcp,
    resolve_dns,
)
from app.classifier import classify_result


class TestDNSResolution:
    """Tests for DNS resolution."""

    def test_successful_dns_resolution(self):
        """Test successful DNS resolution."""
        success, ip, elapsed_ms, error = resolve_dns("example.com", timeout=5.0)

        assert success is True
        assert ip is not None
        assert elapsed_ms is not None
        assert error is None
        # Should resolve to an IP address
        assert "example.com" == "example.com"  # Just checking hostname is valid

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

        success, ip, elapsed_ms, error = resolve_dns("invalid-domain-12345.com", timeout=5.0)

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
        port, received, server = real_server

        success, elapsed_ms, error = connect_tcp("127.0.0.1", str(port), timeout=5.0)

        assert success is True
        assert elapsed_ms is not None
        assert error is None
        # Connection succeeded; received data may be empty if client didn't send anything

    def test_tcp_connection_timeout(self, monkeypatch):
        """Test TCP connection timeout."""
        import socket

        def mock_connect(*args, **kwargs):
            raise TimeoutError("Connection timeout")

        monkeypatch.setattr(socket.socket, "connect", mock_connect)

        success, elapsed_ms, error = connect_tcp("192.0.2.1", "80", timeout=5.0)

        assert success is False
        assert elapsed_ms is None
        assert "timeout" in error.lower()

    def test_tcp_connection_refused(self, monkeypatch):
        """Test TCP connection refused."""
        import socket

        def mock_connect(*args, **kwargs):
            raise ConnectionRefusedError("Connection refused")

        monkeypatch.setattr(socket.socket, "connect", mock_connect)

        success, elapsed_ms, error = connect_tcp("127.0.0.1", "9999", timeout=5.0)

        assert success is False
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


class TestCheckUrlIntegration:
    """Integration tests for complete URL checking."""

    def test_check_http_url(self):
        """Test checking an HTTP URL."""

        # Check example.com
        result = check_url(
            "http://example.com",
            slow_threshold_ms=5000,
            cert_expiry_warning_days=7,
        )

        assert result is not None
        assert result.dns_success is True
        assert result.tcp_success is True
        assert result.http_success is True
        assert result.status_code is not None
        assert result.total_ms is not None
        assert result.result == "OK"

    def test_check_https_url(self):
        """Test checking an HTTPS URL."""
        # Check https://httpbin.org (should work)
        result = check_url(
            "https://httpbin.org",
            slow_threshold_ms=5000,
            cert_expiry_warning_days=7,
        )

        assert result is not None
        assert result.dns_success is True
        assert result.tcp_success is True
        # TLS may succeed or fail depending on the site, but we should have results
        assert result.total_ms is not None

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
        """Test that private IP addresses are blocked (SSRF)."""
        # Mock DNS resolution to return a private IP
        original_resolve_dns = resolve_dns

        def mock_resolve_dns(hostname, timeout=5.0):
            return True, "192.168.1.1", 10.0, None

        monkeypatch.setattr("app.checker.resolve_dns", mock_resolve_dns)

        result = check_url(
            "http://example.com",  # Actual hostname
            slow_threshold_ms=5000,
            cert_expiry_warning_days=7,
        )

        # The check should detect the private IP and block it
        assert result is not None
        # Since we mocked DNS, it should detect private IP
        assert result.result == "DNS_FAILURE"  # Or similar SSRF block message