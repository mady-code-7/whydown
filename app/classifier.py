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
    cert_expired: bool = False,
    cert_expiring_soon: bool = False,
    cert_invalid: bool = False,
) -> tuple[str, str | None]:
    """Classify the result of a check into a status code and failed stage.

    Classification order matters: earlier checks take precedence.

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
        # Distinguish between timeout and connection refused
        # We don't have the error detail here, so return generic
        return "CONNECTION_REFUSED", "tcp"

    # 3. TLS failure (for HTTPS)
    if not tls_success:
        return "TLS_ERROR", "tls"

    # 4. Certificate issues (if we got cert info)
    if cert_expired:
        return "CERT_EXPIRED", "tls"
    if cert_invalid:
        return "CERT_INVALID", "tls"
    if cert_expiring_soon:
        return "CERT_EXPIRING", "tls"

    # 5. HTTP failure
    if not http_success or status_code is None:
        return "READ_TIMEOUT", "http"

    # 6. HTTP error status codes
    if 400 <= status_code < 500:
        return "HTTP_4XX", "http"
    if 500 <= status_code < 600:
        return "HTTP_5XX", "http"

    # 7. Unexpected status code (not matching expected)
    # The expected status is checked outside; here we assume 200 is expected
    if status_code != 200:
        return "HTTP_4XX", "http"  # Treat as error

    # 8. Keyword check failure
    if keyword_found is False:
        return "KEYWORD_MISSING", "http"

    # 9. Slow response
    if total_ms is not None and total_ms > slow_threshold_ms:
        return "SLOW", None

    # 10. DNS slow check - would need dns_ms parameter
    # Not implemented here as it requires dns_ms value

    # 11. All good
    return "OK", None


def classify_from_error(
    failed_stage: str,
    error_message: str,
    cert_expired: bool = False,
    cert_expiring_soon: bool = False,
    cert_invalid: bool = False,
) -> str:
    """Classify based on the stage that failed and error message.

    This is a fallback when we don't have full success/failure booleans.
    """
    if failed_stage == "dns":
        return "DNS_FAILURE"
    elif failed_stage == "tcp":
        # Try to distinguish from error message
        if "refused" in error_message.lower():
            return "CONNECTION_REFUSED"
        elif "timeout" in error_message.lower() or "timed out" in error_message.lower():
            return "CONNECT_TIMEOUT"
        return "CONNECTION_REFUSED"
    elif failed_stage == "tls":
        if cert_expired:
            return "CERT_EXPIRED"
        if cert_invalid:
            return "CERT_INVALID"
        if cert_expiring_soon:
            return "CERT_EXPIRING"
        return "TLS_ERROR"
    elif failed_stage == "http":
        if "timeout" in error_message.lower() or "timed out" in error_message.lower():
            return "READ_TIMEOUT"
        return "HTTP_5XX"
    return "OK"