"""Failure classification functions for WhyDown.

Implements the failure classification from spec section 8.3.
"""
from __future__ import annotations


def classify_result(
    dns_success: bool,
    tcp_success: bool,
    tls_success: bool,
    http_success: bool,
    status_code: int | None,
    ttfb_ms: float | None,
    total_ms: float | None,
    slow_threshold_ms: int,
    keyword_found: bool | None = None,
    expected_status: int = 200,
    cert_expired: bool = False,
    cert_expiring_soon: bool = False,
    cert_invalid: bool = False,
    tcp_error: str | None = None,
) -> tuple[str, str | None]:
    """Classify the result of a check into a status code and failed stage.

    Classification order matters: earlier checks take precedence.

    Args:
        dns_success: Whether DNS resolution succeeded
        tcp_success: Whether TCP connection succeeded
        tls_success: Whether TLS handshake succeeded (True for HTTP)
        http_success: Whether HTTP request/response succeeded
        status_code: HTTP status code, or None if no response
        ttfb_ms: Time to first byte in milliseconds
        total_ms: Total response time in milliseconds
        slow_threshold_ms: Threshold for SLOW classification (ms)
        keyword_found: True if expected keyword present, False if missing, None if not checked
        expected_status: Expected HTTP status (default 200). A matching 3xx is OK.
        cert_expired: True if certificate expired
        cert_expiring_soon: True if certificate expires within warning period
        cert_invalid: True if certificate invalid (hostname mismatch, untrusted, etc.)
        tcp_error: Optional error message from TCP stage to distinguish timeout/refused

    Returns:
        Tuple of (result_code, failed_stage)
        - result_code: One of the classification codes from spec section 8.3
        - failed_stage: One of 'dns', 'tcp', 'tls', 'http', or None if OK/SLOW
    """
    # 1. DNS failure
    if not dns_success:
        return "DNS_FAILURE", "dns"

    # 2. TCP connection failure
    if not tcp_success:
        if tcp_error and "timeout" in tcp_error.lower():
            return "CONNECT_TIMEOUT", "tcp"
        if tcp_error and "refused" in tcp_error.lower():
            return "CONNECTION_REFUSED", "tcp"
        # Default to refused when we can't tell
        return "CONNECTION_REFUSED", "tcp"

    # 3. TLS failure (for HTTPS)
    if not tls_success:
        return "TLS_ERROR", "tls"

    # 4. Certificate issues (if we got cert info)
    if cert_expired:
        return "CERT_EXPIRED", "tls"
    if cert_invalid:
        return "CERT_INVALID", "tls"
    # cert_expiring_soon is a flag (cert_expiring_soon + cert_expires_at), not a result code.
    # The check is still OK/SLOW; alerting logic handles the warning separately.

    # 5. HTTP failure (no response / read timeout / dropped connection)
    if not http_success or status_code is None:
        return "READ_TIMEOUT", "http"

    # 6. 3xx: no redirect followed. OK only if it matches expected_status.
    if 300 <= status_code < 400:
        if status_code == expected_status:
            pass  # 3xx matches expectation -> continue to keyword/slow/OK
        else:
            return "HTTP_3XX", "http"

    # 7. HTTP error status codes
    if 400 <= status_code < 500:
        return "HTTP_4XX", "http"
    if 500 <= status_code < 600:
        return "HTTP_5XX", "http"

    # 8. Unexpected status code (not matching expected)
    if status_code != expected_status:
        return "HTTP_4XX", "http"

    # 9. Keyword check failure
    if keyword_found is False:
        return "KEYWORD_MISSING", "http"

    # 10. Slow response
    if total_ms is not None and total_ms > slow_threshold_ms:
        return "SLOW", None

    # 11. All good
    return "OK", None