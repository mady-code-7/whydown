"""Tests for the individual network stages of the checker (spec section 10).

Every stage is a small function with a single job, so each of them is
tested on its own. The interesting cases are the error branches: a healthy
run never reaches them, but they decide whether a broken site is reported
as the right kind of failure.
"""

from __future__ import annotations

import socket
import ssl
import threading
import time
from contextlib import contextmanager, suppress

import pytest

from app.checker import (
    check_certificate,
    connect_tcp,
    do_http,
    do_tls,
    parse_url,
    resolve_dns,
)

#: The local test servers listen on the loopback interface.
HOST = "127.0.0.1"

#: The name the client asks for in the TLS handshake.
SERVER_NAME = "localhost"


@contextmanager
def _serving(handler):
    """Run a TCP server that hands every accepted socket to ``handler``.

    Yields the port the server listens on. The server is stopped when the
    block is left.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    port = server.getsockname()[1]
    stop = threading.Event()

    def run(conn):
        try:
            handler(conn)
        except OSError:
            # The client went away: nothing to report.
            pass
        finally:
            with suppress(OSError):
                conn.close()

    def accept_loop():
        server.settimeout(0.05)
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=run, args=(conn,), daemon=True).start()

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        with suppress(OSError):
            server.close()
        thread.join(timeout=2)


def _not_after(offset_days: float) -> str:
    """A 'notAfter' value in the OpenSSL format, offset_days from now."""
    return time.strftime("%b %d %H:%M:%S %Y GMT", time.gmtime(time.time() + offset_days * 86400))


class TestParseUrlErrors:
    """A URL that cannot be used is rejected before anything is resolved."""

    def test_missing_hostname_is_rejected(self):
        ok, _scheme, _host, _port, _path, error, _hint = parse_url("http:///ok")

        assert ok is False
        assert error == "Missing hostname"

    def test_unsupported_scheme_is_rejected(self):
        ok, _scheme, _host, _port, _path, error, _hint = parse_url("ftp://monitored.test/ok")

        assert ok is False
        assert "scheme" in error.lower()

    def test_invalid_port_is_rejected(self):
        """A port that is not a number is reported, not raised."""
        ok, _scheme, _host, _port, _path, error, _hint = parse_url(
            "http://monitored.test:not-a-port/ok"
        )

        assert ok is False
        assert error is not None
        assert "port" in error.lower()

    def test_relative_path_is_made_absolute(self, monkeypatch):
        """A path without a leading slash is normalised.

        urlparse() returns an absolute path for every URL that has a host,
        so this branch is a safety net. It is pinned here so that it cannot
        rot unnoticed.
        """

        class _Parsed:
            scheme = "http"
            hostname = "monitored.test"
            port = None
            path = "relative/path"
            query = ""

        monkeypatch.setattr("app.checker.urlparse", lambda url: _Parsed())

        ok, _scheme, _host, _port, path, error, _hint = parse_url("http://monitored.test")

        assert ok is True
        assert path == "/relative/path"
        assert error is None


class TestResolveDnsErrors:
    """Every resolver failure is a DNS failure."""

    def test_no_addresses_found(self, monkeypatch):
        """A resolver that returns nothing is a DNS failure."""
        monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [])

        success, ip, elapsed_ms, error = resolve_dns("monitored.test")

        assert success is False
        assert ip is None
        assert elapsed_ms is not None
        assert error == "No addresses found"

    @pytest.mark.parametrize(
        "error",
        [
            OSError("resolver unavailable"),
            UnicodeError("idna encoding failed"),
        ],
    )
    def test_other_resolver_errors_are_dns_failures(self, monkeypatch, error):
        """A broken resolver is still a DNS failure, not a crash."""
        def boom(*args, **kwargs):
            raise error

        monkeypatch.setattr(socket, "getaddrinfo", boom)

        success, ip, elapsed_ms, message = resolve_dns("monitored.test")

        assert success is False
        assert ip is None
        assert elapsed_ms is None
        assert message == str(error)


class TestConnectTcpErrors:
    """Every TCP failure is reported with a usable message."""

    def test_generic_os_error_is_a_connection_failure(self, monkeypatch):
        def boom(self, address):
            raise OSError("Network is unreachable")

        monkeypatch.setattr(socket.socket, "connect", boom)

        success, sock, elapsed_ms, error = connect_tcp("93.184.216.34", "80")

        assert success is False
        assert sock is None
        assert elapsed_ms is None
        assert "Connection failed" in error

    def test_refused_message_in_an_os_error_is_a_refusal(self, monkeypatch):
        """Some systems report a refusal as a plain OSError."""
        def boom(self, address):
            raise OSError("Connection refused")

        monkeypatch.setattr(socket.socket, "connect", boom)

        success, _sock, _elapsed_ms, error = connect_tcp("93.184.216.34", "80")

        assert success is False
        assert error == "Connection refused"

    def test_invalid_port_is_reported(self):
        """A port that is not a number never reaches the network."""
        success, sock, elapsed_ms, error = connect_tcp("93.184.216.34", "not-a-port")

        assert success is False
        assert sock is None
        assert elapsed_ms is None
        assert "Invalid port" in error


class TestCheckCertificate:
    """The expiry date of a certificate decides the flags."""

    def test_missing_expiry_date_yields_no_flags(self):
        expired, expiring_soon, expires_at = check_certificate(None)

        assert expired is False
        assert expiring_soon is False
        assert expires_at is None

    def test_expired_certificate_is_flagged_as_expired(self):
        expired, expiring_soon, expires_at = check_certificate(_not_after(-1))

        assert expired is True
        assert expiring_soon is False
        assert expires_at is not None

    def test_certificate_inside_the_warning_window_is_flagged(self):
        expired, expiring_soon, expires_at = check_certificate(_not_after(3), warning_days=7)

        assert expired is False
        assert expiring_soon is True
        assert expires_at is not None

    def test_certificate_outside_the_warning_window_is_not_flagged(self):
        expired, expiring_soon, expires_at = check_certificate(_not_after(30), warning_days=7)

        assert expired is False
        assert expiring_soon is False
        assert expires_at is not None

    def test_unreadable_expiry_date_yields_no_flags(self):
        expired, expiring_soon, expires_at = check_certificate("not a date")

        assert expired is False
        assert expiring_soon is False
        assert expires_at is None


class _FailingContext:
    """An SSL context whose handshake always fails with a given error."""

    def __init__(self, error):
        self._error = error

    def wrap_socket(self, *args, **kwargs):
        raise self._error


class TestDoTlsErrors:
    """A failed handshake is reported with the reason it failed."""

    def test_server_that_does_not_speak_tls(self):
        """A server that answers the handshake with something else."""
        def handler(conn):
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

        with (
            _serving(handler) as port,
            socket.create_connection((HOST, port), timeout=5) as sock,
        ):
            success, ssl_sock, _elapsed, error, *_rest = do_tls(sock, SERVER_NAME)

        assert success is False
        assert ssl_sock is None
        assert error is not None
        assert "TLS error" in error

    def test_handshake_timeout(self):
        """A server that never answers the handshake times out."""
        with (
            _serving(lambda conn: time.sleep(3)) as port,
            socket.create_connection((HOST, port), timeout=5) as sock,
        ):
            sock.settimeout(0.5)
            success, ssl_sock, _elapsed, error, *_rest = do_tls(sock, SERVER_NAME)

        assert success is False
        assert ssl_sock is None
        assert error == "TLS handshake timeout"

    @pytest.mark.parametrize(
        "error",
        [
            OSError("connection reset by peer"),
            ValueError("not a socket"),
        ],
    )
    def test_socket_error_during_the_handshake(self, monkeypatch, error):
        """A socket level failure is reported with its message."""
        monkeypatch.setattr(
            ssl, "create_default_context", lambda *a, **k: _FailingContext(error)
        )

        with socket.socket() as sock:
            success, ssl_sock, _elapsed, message, *_rest = do_tls(sock, "localhost")

        assert success is False
        assert ssl_sock is None
        assert message == str(error)


class TestDoHttp:
    """The HTTP stage reports a broken response instead of crashing."""

    def test_malformed_status_line(self):
        """A response that is not an HTTP response is not a success."""
        def handler(conn):
            conn.recv(4096)
            conn.sendall(b"GARBAGE\r\n\r\n")

        with (
            _serving(handler) as port,
            socket.create_connection((HOST, port), timeout=5) as sock,
        ):
            success, status, _size, _ttfb, _total, body, error = do_http(
                sock, "/", HOST, timeout=5
            )

        assert success is False
        assert status is None
        assert body is None
        assert error == "Invalid response: GARBAGE"

    def test_oversized_body_is_truncated(self):
        """A body bigger than the limit is cut off at the limit."""
        payload = b"x" * 50

        def handler(conn):
            conn.recv(4096)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\nConnection: close\r\n\r\n" + payload
            )

        with (
            _serving(handler) as port,
            socket.create_connection((HOST, port), timeout=5) as sock,
        ):
            success, status, size, _ttfb, _total, body, error = do_http(
                sock, "/", HOST, timeout=5, max_body_size=10
            )

        assert success is True
        assert error is None
        assert status == 200
        assert size == 10
        assert body == payload[:10].decode("utf-8")
