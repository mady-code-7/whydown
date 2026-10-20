"""Certificate material and a local TLS server for the WhyDown test suite.

The certificate test cases of spec section 13.3 (T07, T08, T09) need real
certificates: one that has expired, one that does not cover the host the
client asks for and one that is signed by nobody the client trusts. They are
created here with the ``cryptography`` package, inside the test run, and are
never written into the repository.

Every leaf certificate is issued by a throwaway certificate authority that
exists only for the duration of the test session. The client side of the
tests trusts that CA through the ``trust_test_ca`` fixture, which
monkeypatches ``ssl.create_default_context``. The replacement context keeps
``check_hostname`` enabled and the verification mode at ``CERT_REQUIRED``:
the application code still refuses a broken certificate, it only accepts the
test CA as an additional trust anchor.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import socket
import ssl
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

#: The TLS server listens on the loopback interface.
HOST = "127.0.0.1"

#: The name the client asks for. The server certificate covers this name, so
#: a mismatch is only produced by the certificates that deliberately do not
#: cover it.
SERVER_NAME = "localhost"

#: The answer of the local TLS server.
RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"

#: Key size of the generated keys. 2048 bits is the smallest size that is
#: still accepted everywhere, and generating them is fast enough for a test
#: suite.
KEY_SIZE = 2048


def _utcnow() -> dt.datetime:
    """Current UTC time without a timezone (what the builder expects)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _common_name(value: str) -> x509.Name:
    """A distinguished name with a single common name."""
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, value)])


def _ca_key_usage() -> x509.KeyUsage:
    """Key usage of a certificate authority."""
    return x509.KeyUsage(
        digital_signature=False,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=True,
        crl_sign=True,
        encipher_only=False,
        decipher_only=False,
    )


def _server_key_usage() -> x509.KeyUsage:
    """Key usage of a TLS server certificate."""
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=True,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=False,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False,
    )


def _local_names() -> list[x509.GeneralName]:
    """The names the test server certificate covers."""
    return [
        x509.DNSName(SERVER_NAME),
        x509.IPAddress(ipaddress.ip_address(HOST)),
    ]


@dataclass(frozen=True)
class CertificateFiles:
    """A certificate and its private key, written to disk as PEM."""

    cert_path: Path
    key_path: Path

    def __fspath__(self) -> str:
        return str(self.cert_path)


@dataclass(frozen=True)
class CertificateAuthority:
    """The throwaway CA that signs the leaf certificates of the tests."""

    key: rsa.RSAPrivateKey
    cert: x509.Certificate
    files: CertificateFiles


def _write_pem(
    cert: x509.Certificate,
    key: rsa.RSAPrivateKey,
    directory: Path,
    name: str,
) -> CertificateFiles:
    """Write a certificate and its key into ``directory``."""
    cert_path = directory / f"{name}.pem"
    key_path = directory / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return CertificateFiles(cert_path=cert_path, key_path=key_path)


def build_ca(directory: Path) -> CertificateAuthority:
    """Create a throwaway certificate authority."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    name = _common_name("WhyDown Test CA")
    now = _utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(_ca_key_usage(), critical=True)
        .sign(key, hashes.SHA256())
    )
    return CertificateAuthority(
        key=key,
        cert=cert,
        files=_write_pem(cert, key, directory, "test-ca"),
    )


def build_leaf(
    ca: CertificateAuthority,
    directory: Path,
    name: str,
    not_before: dt.datetime,
    not_after: dt.datetime,
    names: list[x509.GeneralName] | None = None,
) -> CertificateFiles:
    """Issue a server certificate signed by the test CA."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_common_name(SERVER_NAME))
        .issuer_name(ca.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_server_key_usage(), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(names if names is not None else _local_names()),
            critical=False,
        )
        .sign(ca.key, hashes.SHA256())
    )
    return _write_pem(cert, key, directory, name)


def build_self_signed(directory: Path, name: str = "self-signed") -> CertificateFiles:
    """Create a certificate that is signed by nobody the client trusts."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    now = _utcnow()
    name_ = _common_name(SERVER_NAME)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name_)
        .issuer_name(name_)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_server_key_usage(), critical=True)
        .add_extension(x509.SubjectAlternativeName(_local_names()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return _write_pem(cert, key, directory, name)


class TLSServer:
    """A TLS server on the loopback interface that presents one certificate.

    The server context is a plain server context: it never asks for a client
    certificate, so there is no client side verification to disable. The
    certificate it presents is the only interesting part - it is expired, it
    covers the wrong host or it is self-signed, depending on the test.
    """

    def __init__(self, certificate: CertificateFiles, host: str = HOST) -> None:
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(certificate.cert_path, certificate.key_path)
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, 0))
        self._sock.listen(8)
        self._sock.settimeout(0.05)
        self.host = host
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def url(self, path: str = "/") -> str:
        """HTTPS URL of this server."""
        if not path.startswith("/"):
            path = "/" + path
        return f"https://{SERVER_NAME}:{self.port}{path}"

    def stop(self) -> None:
        """Stop the server and release the port."""
        self._stop.set()
        with suppress(OSError):
            self._sock.close()
        self._thread.join(timeout=2)

    def _accept_loop(self) -> None:
        """Accept connections until the server is stopped."""
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except TimeoutError:
                # Only means "no client yet".
                continue
            except OSError:
                # The listening socket was closed by stop().
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        """Serve a single TLS connection."""
        try:
            conn.settimeout(10.0)
            with self._context.wrap_socket(conn, server_side=True) as tls_conn:
                tls_conn.recv(4096)
                tls_conn.sendall(RESPONSE)
        except (ssl.SSLError, OSError):
            # The client rejected our certificate. That is the expected
            # outcome of the failure cases, so there is nothing to report.
            with suppress(OSError):
                conn.close()


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture(scope="session")
def cert_authority(tmp_path_factory) -> CertificateAuthority:
    """The throwaway CA that signs the leaf certificates."""
    directory = tmp_path_factory.mktemp("cert-authority")
    return build_ca(directory)


@pytest.fixture
def trust_test_ca(monkeypatch, cert_authority):
    """Let the checker trust the test CA (and only the test CA).

    The replacement context verifies the peer exactly as the production one
    does: the hostname is checked and an untrusted certificate is rejected.
    Only the trust anchor differs.
    """
    def create_default_context(*args, **kwargs):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=str(cert_authority.files.cert_path))
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    monkeypatch.setattr(ssl, "create_default_context", create_default_context)
    return cert_authority


@pytest.fixture(scope="session")
def expired_certificate(cert_authority, tmp_path_factory) -> CertificateFiles:
    """A certificate whose validity ended yesterday."""
    directory = tmp_path_factory.mktemp("cert-expired")
    now = _utcnow()
    return build_leaf(
        cert_authority,
        directory,
        "expired",
        not_before=now - dt.timedelta(days=30),
        not_after=now - dt.timedelta(days=1),
    )


@pytest.fixture(scope="session")
def hostname_mismatch_certificate(cert_authority, tmp_path_factory) -> CertificateFiles:
    """A valid certificate that does not cover the name the client asks for."""
    directory = tmp_path_factory.mktemp("cert-mismatch")
    now = _utcnow()
    return build_leaf(
        cert_authority,
        directory,
        "hostname-mismatch",
        not_before=now - dt.timedelta(days=1),
        not_after=now + dt.timedelta(days=365),
        names=[x509.DNSName("other.example.com")],
    )


@pytest.fixture(scope="session")
def self_signed_certificate(tmp_path_factory) -> CertificateFiles:
    """A certificate that is signed by itself and by nobody else."""
    directory = tmp_path_factory.mktemp("cert-self-signed")
    return build_self_signed(directory)


@pytest.fixture(scope="session")
def expiring_soon_certificate(cert_authority, tmp_path_factory) -> CertificateFiles:
    """A valid certificate that expires in three days."""
    directory = tmp_path_factory.mktemp("cert-expiring-soon")
    now = _utcnow()
    return build_leaf(
        cert_authority,
        directory,
        "expiring-soon",
        not_before=now - dt.timedelta(days=1),
        not_after=now + dt.timedelta(days=3),
    )


@pytest.fixture(scope="session")
def valid_certificate(cert_authority, tmp_path_factory) -> CertificateFiles:
    """A valid certificate that is good for another year."""
    directory = tmp_path_factory.mktemp("cert-valid")
    now = _utcnow()
    return build_leaf(
        cert_authority,
        directory,
        "valid",
        not_before=now - dt.timedelta(days=1),
        not_after=now + dt.timedelta(days=365),
    )


@pytest.fixture
def tls_server():
    """Factory that starts a TLS server presenting the given certificate."""
    servers: list[TLSServer] = []

    def start(certificate: CertificateFiles) -> TLSServer:
        server = TLSServer(certificate)
        servers.append(server)
        return server

    yield start

    for server in servers:
        server.stop()
