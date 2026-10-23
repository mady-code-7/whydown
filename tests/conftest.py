"""Shared pytest fixtures for the WhyDown test suite.

The autouse ``no_real_network`` fixture makes the suite hermetic: any attempt
to open a connection to a non-loopback address, or to resolve a hostname
other than localhost, fails immediately. A future test can therefore never
silently depend on the real internet - it has to use the local mock server on
127.0.0.1 or monkeypatch the stage functions.

The autouse ``dotenv_calls`` fixture does the same for the .env file: the
command line entry point of ``app.db`` loads it, but during a test run the
real loader is replaced by a recorder, so no real .env file (and no real
secret) is ever read.

Tests that already monkeypatch ``socket.getaddrinfo`` or ``socket.socket.connect``
keep working: the test's own patch is applied after this fixture and therefore
takes precedence.
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

# ``load_test.py`` is a runnable script (``python -m tests.load_test``), not a
# pytest module: it starts a real mock server and runs the check runner, so it
# must never be collected or executed by the test suite. It ends in ``_test.py``
# which matches pytest's default ``python_files`` glob, so it is listed here
# explicitly. The file itself is not named ``test_*`` and contains no test
# functions, but without this entry pytest would still import it during
# collection and fail on the module-level imports.
collect_ignore = ["load_test.py"]

# The certificate material and the local TLS server live in their own module;
# the fixtures are re-exported here so that every test can simply ask for
# "tls_server" or "trust_test_ca".
from certificates import (  # noqa: F401
    cert_authority,
    expired_certificate,
    expiring_soon_certificate,
    hostname_mismatch_certificate,
    self_signed_certificate,
    tls_server,
    trust_test_ca,
    valid_certificate,
)

# The mock failure server and its fixture live in their own module; the
# fixture is re-exported here so that every test can simply ask for
# "mock_server".
from mock_server import mock_server  # noqa: F401

# Host names that may be resolved during a test run. "localhost" resolves from
# the local hosts file, so it never touches the network.
LOCAL_HOSTNAMES = frozenset(
    {
        "",
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
    }
)


class RealNetworkAccessError(BaseException):
    """Raised when a test tries to use the real network.

    It derives from BaseException on purpose: the application code catches
    ``Exception`` very broadly (a failing check must never break a run), and
    this error must not be swallowed by that safety net. It has to reach
    pytest so the offending test fails loudly.
    """


def _is_local_target(host: object) -> bool:
    """Return True if the given host refers to the local machine."""
    if host is None:
        # getaddrinfo(None, ...) means "the local host".
        return True
    if isinstance(host, bytes):
        host = host.decode("utf-8", errors="replace")
    if not isinstance(host, str):
        return False
    if host in LOCAL_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _describe(host: object) -> str:
    """Human readable description of a host argument."""
    if isinstance(host, bytes):
        return host.decode("utf-8", errors="replace")
    return str(host)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Block DNS lookups and connections to anything but the loopback device."""
    real_getaddrinfo = socket.getaddrinfo
    real_gethostbyname = socket.gethostbyname
    real_connect = socket.socket.connect
    real_create_connection = socket.create_connection

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        if _is_local_target(host):
            return real_getaddrinfo(host, port, *args, **kwargs)
        raise RealNetworkAccessError(
            f"Real DNS lookup of {_describe(host)!r} is not allowed in tests. "
            "Monkeypatch socket.getaddrinfo or app.checker.resolve_dns instead."
        )

    def guarded_gethostbyname(host, *args, **kwargs):
        if _is_local_target(host):
            return real_gethostbyname(host, *args, **kwargs)
        raise RealNetworkAccessError(
            f"Real DNS lookup of {_describe(host)!r} is not allowed in tests."
        )

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if _is_local_target(host):
            return real_connect(self, address)
        raise RealNetworkAccessError(
            f"Real connection to {_describe(host)!r} is not allowed in tests. "
            "Use a loopback mock server or monkeypatch app.checker.connect_tcp."
        )

    def guarded_create_connection(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if not _is_local_target(host):
            raise RealNetworkAccessError(
                f"Real connection to {_describe(host)!r} is not allowed in tests. "
                "Use a loopback mock server or monkeypatch app.checker.connect_tcp."
            )
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", guarded_gethostbyname)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)

    yield


@pytest.fixture(autouse=True)
def dotenv_calls(monkeypatch):
    """Replace the .env loader with a recorder, so no real .env is read.

    ``app.db`` and ``app.alerts`` call ``load_dotenv()`` on their command line
    entry points only. During a test run the call is intercepted here and
    recorded instead, which keeps the suite hermetic - a real .env file in the
    working directory is never opened and no real secret ever reaches the
    process environment.

    A test that wants to assert on the loading behaviour asks for this
    fixture by name and inspects the returned list of calls.
    """
    from app import alerts, db

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def recording_load_dotenv(*args: object, **kwargs: object) -> bool:
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(db, "load_dotenv", recording_load_dotenv)
    monkeypatch.setattr(alerts, "load_dotenv", recording_load_dotenv)

    return calls
