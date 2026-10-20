"""Certificate tests: T07, T08 and T09 of spec section 13.3.

The certificates are real ones, generated inside the test run (see
``tests/certificates.py``) and presented by a TLS server on the loopback
interface. The client trusts the test CA through a monkeypatched
``ssl.create_default_context``, so the application code keeps verifying the
hostname and the chain exactly as it does in production.
"""

from __future__ import annotations

import datetime as dt

from app.checker import NetworkTimeouts, check_url

#: Short timeouts: the certificate tests talk to a server on the loopback
#: interface, so every stage answers immediately.
FAST_TIMEOUTS = NetworkTimeouts(
    dns_timeout=2.0,
    connect_timeout=2.0,
    tls_timeout=5.0,
    read_timeout=5.0,
    total_timeout=10.0,
)

#: The warning window used by the tests. It is a parameter of the check, not
#: a constant of the application.
WARNING_DAYS = 7


def _check(url: str, **kwargs):
    """Run a check against the local TLS server.

    The server is on the loopback interface, which the SSRF filter blocks
    unless the test explicitly opts in.
    """
    kwargs.setdefault("timeouts", FAST_TIMEOUTS)
    kwargs.setdefault("allow_private", True)
    kwargs.setdefault("cert_expiry_warning_days", WARNING_DAYS)
    return check_url(url, **kwargs)


class TestT07ExpiredCertificate:
    """T07: an expired certificate is reported as CERT_EXPIRED."""

    def test_expired_certificate_is_reported_as_cert_expired(
        self, tls_server, expired_certificate, trust_test_ca
    ):
        server = tls_server(expired_certificate)

        result = _check(server.url("/"))

        assert result.result == "CERT_EXPIRED"
        assert result.failed_stage == "tls"
        assert result.tls_success is False
        assert result.cert_expired is True
        assert result.cert_invalid is False
        assert result.cert_expiring_soon is False
        # The handshake failed, so there is no HTTP response at all.
        assert result.status_code is None
        assert result.http_success is False

    def test_expired_certificate_reports_the_verification_error(
        self, tls_server, expired_certificate, trust_test_ca
    ):
        """The reason from OpenSSL is kept, so the dashboard can show it."""
        server = tls_server(expired_certificate)

        result = _check(server.url("/"))

        assert result.error_message is not None
        assert "Certificate error" in result.error_message


class TestT08HostnameMismatch:
    """T08: a certificate for another host is reported as CERT_INVALID."""

    def test_certificate_for_another_host_is_reported_as_cert_invalid(
        self, tls_server, hostname_mismatch_certificate, trust_test_ca
    ):
        server = tls_server(hostname_mismatch_certificate)

        result = _check(server.url("/"))

        assert result.result == "CERT_INVALID"
        assert result.failed_stage == "tls"
        assert result.tls_success is False
        assert result.cert_invalid is True
        assert result.cert_expired is False
        assert result.status_code is None

    def test_self_signed_certificate_is_reported_as_cert_invalid(
        self, tls_server, self_signed_certificate, trust_test_ca
    ):
        """A certificate signed by nobody the client trusts is invalid too."""
        server = tls_server(self_signed_certificate)

        result = _check(server.url("/"))

        assert result.result == "CERT_INVALID"
        assert result.failed_stage == "tls"
        assert result.cert_invalid is True
        assert result.cert_expired is False
        assert result.status_code is None


class TestT09ExpiringSoon:
    """T09: a certificate that is about to expire is only flagged."""

    def test_certificate_expiring_soon_keeps_the_site_healthy(
        self, tls_server, expiring_soon_certificate, trust_test_ca
    ):
        server = tls_server(expiring_soon_certificate)

        result = _check(server.url("/"))

        # The handshake works, so the check is a success...
        assert result.result == "OK"
        assert result.failed_stage is None
        assert result.status_code == 200
        assert result.tls_success is True
        assert result.cert_expired is False
        assert result.cert_invalid is False
        # ...but the certificate is flagged as about to expire.
        assert result.cert_expiring_soon is True
        assert result.cert_expires_at is not None

    def test_expiry_date_is_recorded_as_a_timestamp(
        self, tls_server, expiring_soon_certificate, trust_test_ca
    ):
        """The expiry is stored as an absolute, timezone aware timestamp."""
        server = tls_server(expiring_soon_certificate)

        result = _check(server.url("/"))

        assert result.cert_expires_at is not None
        assert result.cert_expires_at.tzinfo is not None
        # The certificate expires in three days, so it has to expire inside
        # the warning window of seven days and not before now.
        remaining = result.cert_expires_at - dt.datetime.now(dt.UTC)
        assert dt.timedelta(0) < remaining <= dt.timedelta(days=WARNING_DAYS)

    def test_warning_window_is_configurable(
        self, tls_server, expiring_soon_certificate, trust_test_ca
    ):
        """A wider warning window still flags the certificate."""
        server = tls_server(expiring_soon_certificate)

        result = _check(server.url("/"), cert_expiry_warning_days=30)

        assert result.cert_expiring_soon is True
        assert result.result == "OK"


class TestHealthyTlsSite:
    """A site with a valid certificate is healthy and fully described."""

    def test_valid_certificate_yields_a_healthy_check(
        self, tls_server, valid_certificate, trust_test_ca
    ):
        server = tls_server(valid_certificate)

        result = _check(server.url("/"))

        assert result.result == "OK"
        assert result.status_code == 200
        assert result.tls_success is True
        assert result.cert_expired is False
        assert result.cert_invalid is False
        assert result.cert_expiring_soon is False
        assert result.cert_expires_at is not None
        assert result.cert_info is not None
        assert result.cert_info["issuer"]
        assert result.cert_info["subject"]

    def test_tls_stage_is_timed(self, tls_server, valid_certificate, trust_test_ca):
        """The handshake is a stage of its own and is timed."""
        server = tls_server(valid_certificate)

        result = _check(server.url("/"))

        assert result.tls_ms is not None
        assert result.tls_ms >= 0
        assert result.total_ms >= result.tls_ms
