"""Mock failure server for the WhyDown test suite (spec section 13.2).

A small HTTP server bound to 127.0.0.1 on a random free port. It can be made
slow, broken or silent on demand, so the real checker can be exercised
against real sockets without ever touching the public internet.

Endpoints:

===============  ==========================================================
``/ok``          200 with the marker text, returned immediately
``/slow?ms=250`` 200 after a delay of ``ms`` milliseconds
``/status/500``  the given status code (e.g. ``/status/404``)
``/drop``        the connection is closed without any reply
``/hang``        the connection is accepted but never answered
``/keyword-missing``  200 with a body that does not contain the marker
===============  ==========================================================

The whole mock site can be switched ``down`` (``set_down()``) and back ``up``
again (``set_up()``). While it is down every request is answered with
HTTP 503, which is how a real site that is still up but broken looks to a
monitor.

The server is started and stopped by the ``mock_server`` pytest fixture
(re-exported from ``tests/conftest.py``).
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import suppress
from urllib.parse import parse_qs, urlparse

import pytest

HOST = "127.0.0.1"

#: Marker text present in a healthy body and absent from /keyword-missing.
#: Tests use it as the expected keyword.
MARKER = "WHYDOWN-OK-MARKER"

#: Upper bound for the /slow delay, so a test can never hang the suite.
MAX_SLOW_MS = 5_000

#: Default delay of /slow when no (or an invalid) ms parameter is given.
#: It is deliberately short: a check against "/slow" without a query string
#: gets this delay, while "/slow?ms=250" gets exactly 250 ms because the
#: checker forwards the query string.
DEFAULT_SLOW_MS = 250

#: How long the accept loop waits for a new connection before it checks the
#: stop flag again. Small enough to keep the response time of the mock
#: server well inside the short timeouts of the test suite.
ACCEPT_POLL_SECONDS = 0.05

_REASONS = {
    200: "OK",
    204: "No Content",
    301: "Moved Permanently",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


class MockServer:
    """A controllable HTTP server on the loopback interface.

    The server runs in a daemon thread and never blocks the test process on
    teardown: ``stop()`` closes the listening socket, which ends the accept
    loop.
    """

    def __init__(self, host: str = HOST, slow_ms: int = DEFAULT_SLOW_MS) -> None:
        self._host = host
        self._slow_ms = slow_ms
        self._down = False
        self._stop = threading.Event()
        self._sock = self._bind()
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _bind(self) -> socket.socket:
        """Bind a listening socket to a random free port on the loopback."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._host, 0))
        sock.listen(32)
        return sock

    # ------------------------------------------------------------------
    # URLs
    # ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        """Base URL of the server, e.g. ``http://127.0.0.1:54321``."""
        return f"http://{self._host}:{self.port}"

    def url(self, path: str) -> str:
        """Full URL for a path on this server."""
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    # ------------------------------------------------------------------
    # down / up switch
    # ------------------------------------------------------------------
    def set_down(self) -> None:
        """Make the mock site report itself as down (every path: HTTP 503)."""
        self._down = True

    def set_up(self) -> None:
        """Bring the mock site back up."""
        self._down = False

    @property
    def is_down(self) -> bool:
        """True while the mock site is switched down."""
        return self._down

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Stop the server and release the port."""
        self._stop.set()
        with suppress(OSError):
            self._sock.close()
        self._thread.join(timeout=2)

    def _accept_loop(self) -> None:
        """Accept connections until the server is stopped."""
        self._sock.settimeout(ACCEPT_POLL_SECONDS)
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                # TimeoutError is the timeout of the accept call; it only
                # means "no client yet", so keep waiting.
                continue
            except OSError:
                # The listening socket was closed by stop().
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    # ------------------------------------------------------------------
    # request handling
    # ------------------------------------------------------------------
    def _handle(self, conn: socket.socket) -> None:
        """Serve a single connection."""
        try:
            conn.settimeout(10.0)
            request = self._read_request(conn)
            if request is None:
                return
            _method, target = request
            parsed = urlparse(target)
            self._route(conn, parsed.path, parse_qs(parsed.query))
        except OSError:
            # The client went away mid-request: nothing to report.
            pass
        finally:
            with suppress(OSError):
                conn.close()

    @staticmethod
    def _read_request(conn: socket.socket) -> tuple[str, str] | None:
        """Read the request line and return (method, target)."""
        data = b""
        while b"\r\n" not in data and len(data) < 8192:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        line = data.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
        parts = line.split(" ")
        if len(parts) < 2:
            return None
        return parts[0], parts[1]

    def _route(self, conn: socket.socket, path: str, query: dict[str, list[str]]) -> None:
        """Dispatch a request to the matching endpoint."""
        if self._down:
            self._respond(conn, 503, "Mock site is down\n")
            return

        if path == "/ok":
            self._respond(conn, 200, f"Mock server OK. {MARKER}\n")
        elif path == "/slow":
            delay_ms = self._slow_delay_ms(query)
            time.sleep(delay_ms / 1000)
            self._respond(conn, 200, f"Mock server OK after {delay_ms} ms. {MARKER}\n")
        elif path.startswith("/status/"):
            self._respond(conn, self._status_from_path(path), "Requested status\n")
        elif path == "/drop":
            # Close the connection without sending anything at all.
            return
        elif path == "/hang":
            self._hang(conn)
        elif path == "/keyword-missing":
            # A page that loads fine but does not contain the marker.
            self._respond(conn, 200, "Service temporarily unavailable - maintenance window\n")
        else:
            self._respond(conn, 404, "Not found\n")

    @staticmethod
    def _hang(conn: socket.socket) -> None:
        """Accept the request and never answer it.

        The connection is held open until the client gives up (and closes it),
        so the client sees a read timeout rather than an empty reply.
        """
        conn.settimeout(10.0)
        try:
            while True:
                if not conn.recv(4096):
                    break
        except OSError:
            pass

    @staticmethod
    def _delay_ms(query: dict[str, list[str]]) -> int:
        """Read the delay of /slow from the ``ms`` query parameter."""
        raw = (query.get("ms") or [""])[0]
        try:
            delay_ms = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_SLOW_MS
        return max(0, min(delay_ms, MAX_SLOW_MS))

    def _slow_delay_ms(self, query: dict[str, list[str]]) -> int:
        """Delay of /slow: the ``ms`` parameter, or the server default."""
        if "ms" not in query:
            return self._slow_ms
        return self._delay_ms(query)

    @staticmethod
    def _status_from_path(path: str) -> int:
        """Read the status code of /status/<code>."""
        raw = path.rsplit("/", 1)[-1]
        try:
            status = int(raw)
        except (TypeError, ValueError):
            return 500
        if not 100 <= status <= 599:
            return 500
        return status

    @staticmethod
    def _respond(conn: socket.socket, status: int, body: str) -> None:
        """Send a complete HTTP response and close the connection."""
        payload = body.encode("utf-8")
        reason = _REASONS.get(status, "Status")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        with suppress(OSError):
            conn.sendall(head + payload)


@pytest.fixture
def mock_server():
    """Run a MockServer for the duration of a test and stop it afterwards."""
    server = MockServer()
    try:
        yield server
    finally:
        server.stop()
