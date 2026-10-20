"""Unit tests for the failure classifier (spec section 8.3).

The classifier is the heart of WhyDown: it turns the outcome of the four
network stages into the status code the dashboard shows and the alerting
logic reacts to. These tests pin down every classification code and the
order in which the rules are applied.
"""

from __future__ import annotations

from app.classifier import classify_result


def _classify(**overrides):
    """Classify a healthy check, with the given fields overridden."""
    kwargs = {
        "dns_success": True,
        "tcp_success": True,
        "tls_success": True,
        "http_success": True,
        "status_code": 200,
        "ttfb_ms": 100.0,
        "total_ms": 200.0,
        "slow_threshold_ms": 5000,
    }
    kwargs.update(overrides)
    return classify_result(**kwargs)


class TestCertificateCodes:
    """A broken certificate has its own codes, not a generic TLS error."""

    def test_expired_certificate_is_cert_expired(self):
        result, stage = _classify(cert_expired=True)

        assert result == "CERT_EXPIRED"
        assert stage == "tls"

    def test_invalid_certificate_is_cert_invalid(self):
        result, stage = _classify(cert_invalid=True)

        assert result == "CERT_INVALID"
        assert stage == "tls"

    def test_expired_wins_over_invalid(self):
        """A certificate that is both expired and invalid is expired."""
        result, stage = _classify(cert_expired=True, cert_invalid=True)

        assert result == "CERT_EXPIRED"
        assert stage == "tls"

    def test_certificate_problem_wins_over_the_http_status(self):
        """The certificate is checked before the response status."""
        result, stage = _classify(cert_expired=True, status_code=500)

        assert result == "CERT_EXPIRED"
        assert stage == "tls"

    def test_expiring_soon_is_only_a_flag(self):
        """A certificate that is about to expire keeps the check healthy."""
        result, stage = _classify(cert_expiring_soon=True)

        assert result == "OK"
        assert stage is None


class TestReadTimeout:
    """A missing answer is a read timeout, whatever the stage reported."""

    def test_failed_http_request_is_read_timeout(self):
        result, stage = _classify(http_success=False, status_code=None)

        assert result == "READ_TIMEOUT"
        assert stage == "http"

    def test_missing_status_code_is_read_timeout(self):
        """A successful request without a status code is not a success."""
        result, stage = _classify(status_code=None)

        assert result == "READ_TIMEOUT"
        assert stage == "http"


class TestUnexpectedStatus:
    """A status the site did not expect is an error, whatever it is."""

    def test_unexpected_no_content_status_is_a_client_error(self):
        """A 2xx that is not the expected status is reported as HTTP_4XX."""
        result, stage = _classify(status_code=204, expected_status=200)

        assert result == "HTTP_4XX"
        assert stage == "http"

    def test_unexpected_informational_status_is_a_client_error(self):
        result, stage = _classify(status_code=100, expected_status=200)

        assert result == "HTTP_4XX"
        assert stage == "http"

    def test_expected_no_content_status_is_healthy(self):
        result, stage = _classify(status_code=204, expected_status=204)

        assert result == "OK"
        assert stage is None


class TestTcpErrorMessages:
    """The TCP error message tells a timeout from a refused connection."""

    def test_refused_message_is_a_refused_connection(self):
        result, stage = _classify(
            dns_success=True,
            tcp_success=False,
            tls_success=False,
            http_success=False,
            status_code=None,
            ttfb_ms=None,
            total_ms=None,
            tcp_error="Connection refused",
        )

        assert result == "CONNECTION_REFUSED"
        assert stage == "tcp"

    def test_timeout_message_is_a_connect_timeout(self):
        result, stage = _classify(
            dns_success=True,
            tcp_success=False,
            tls_success=False,
            http_success=False,
            status_code=None,
            ttfb_ms=None,
            total_ms=None,
            tcp_error="Connection timeout",
        )

        assert result == "CONNECT_TIMEOUT"
        assert stage == "tcp"

    def test_unknown_error_falls_back_to_refused(self):
        """Without a recognisable message the failure is still a TCP one."""
        result, stage = _classify(
            dns_success=True,
            tcp_success=False,
            tls_success=False,
            http_success=False,
            status_code=None,
            ttfb_ms=None,
            total_ms=None,
            tcp_error="Network is unreachable",
        )

        assert result == "CONNECTION_REFUSED"
        assert stage == "tcp"


class TestPrecedence:
    """The order of the rules decides which code wins."""

    def test_dns_failure_wins_over_everything(self):
        result, stage = _classify(
            dns_success=False,
            tcp_success=False,
            tls_success=False,
            http_success=False,
            status_code=None,
            cert_expired=True,
        )

        assert result == "DNS_FAILURE"
        assert stage == "dns"

    def test_tcp_failure_wins_over_the_certificate(self):
        result, stage = _classify(
            tcp_success=False,
            tls_success=False,
            http_success=False,
            status_code=None,
            cert_invalid=True,
        )

        assert result == "CONNECTION_REFUSED"
        assert stage == "tcp"

    def test_tls_failure_wins_over_the_http_status(self):
        result, stage = _classify(tls_success=False, status_code=500)

        assert result == "TLS_ERROR"
        assert stage == "tls"

    def test_keyword_missing_wins_over_a_slow_response(self):
        result, stage = _classify(keyword_found=False, total_ms=6000.0)

        assert result == "KEYWORD_MISSING"
        assert stage == "http"

    def test_slow_dns_wins_over_the_total_time(self):
        """DNS_SLOW is more specific than the generic SLOW."""
        result, stage = _classify(dns_ms=6000.0, total_ms=7000.0)

        assert result == "DNS_SLOW"
        assert stage is None
