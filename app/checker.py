"""Network checking functions for WhyDown.

Implements the stage functions from section 10 of the spec:
- DNS resolution (resolve_dns)
- TCP connection (connect_tcp)
- TLS handshake (do_tls)
- HTTP request/response (do_http)

All functions are pure and can be unit-tested with mocks.
"""

from __future__ import annotations

import logging
import socket
import ssl
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from .classifier import classify_result
from .security import is_private_ip

logger = logging.getLogger(__name__)

# OpenSSL X509_V_ERR_* verification codes. The ssl module does not expose these
# constants, but SSLCertVerificationError.verify_code carries one of them so we
# can tell an expired certificate (CERT_EXPIRED) from any other verification
# failure (CERT_INVALID), e.g. hostname mismatch or an untrusted chain.
X509_V_ERR_CERT_NOT_YET_VALID = 9
X509_V_ERR_CERT_HAS_EXPIRED = 10


@dataclass
class CheckResult:
    """Result of a complete site check."""
    # Success/failure at each stage
    dns_success: bool = False
    tcp_success: bool = False
    tls_success: bool = False
    http_success: bool = False

    # Timing data (milliseconds)
    dns_ms: float | None = None
    tcp_ms: float | None = None
    tls_ms: float | None = None
    ttfb_ms: float | None = None
    # Total time of the whole check (DNS + TCP + TLS + HTTP), not just the
    # HTTP stage: this is the number compared against the slow threshold.
    total_ms: float | None = None

    # HTTP data
    status_code: int | None = None
    response_size: int | None = None
    keyword_found: bool | None = None

    # IP and certificate info
    resolved_ip: str | None = None
    cert_info: dict[str, Any] | None = None
    cert_expires_at: datetime | None = None

    # Error information
    error_message: str | None = None
    failed_stage: str | None = None

    # Classification result
    result: str = "OK"

    # Certificate flags
    cert_expired: bool = False
    cert_expiring_soon: bool = False
    cert_invalid: bool = False


@dataclass
class NetworkTimeouts:
    """Network timeout configuration."""
    dns_timeout: float = 5.0
    connect_timeout: float = 8.0
    tls_timeout: float = 10.0
    read_timeout: float = 15.0
    total_timeout: float = 60.0


def parse_url(url: str) -> tuple[bool, str | None, str | None, str | None, str | None, str | None, str | None]:
    """Parse a URL into its components.

    Returns:
        Tuple of (success, scheme, hostname, port, path, error_message, resolved_ip_hint)
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, None, None, None, None, f"Unsupported scheme: {parsed.scheme}", None

        if not parsed.hostname:
            return False, None, None, None, None, "Missing hostname", None

        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80

        path = parsed.path or "/"
        if not path.startswith("/"):
            path = "/" + path

        return True, parsed.scheme, parsed.hostname, str(port), path, None, None
    except ValueError as e:
        # urlparse raises ValueError for malformed URLs (e.g. a bad port).
        return False, None, None, None, None, str(e), None


def resolve_dns(hostname: str, timeout: float = 5.0) -> tuple[bool, str | None, float | None, str | None]:
    """Resolve hostname to IP address.

    Args:
        hostname: The hostname to resolve
        timeout: DNS resolution timeout in seconds

    Returns:
        Tuple of (success, ip_address, elapsed_ms, error_message)
    """
    try:
        start_time = time.perf_counter()

        # Use getaddrinfo with timeout simulation
        addrinfo = socket.getaddrinfo(
            hostname,
            None,
            socket.AF_INET,
            socket.SOCK_STREAM,
        )

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        if not addrinfo:
            return False, None, elapsed_ms, "No addresses found"

        # Get the first IPv4 address
        ip = addrinfo[0][4][0]

        return True, ip, elapsed_ms, None
    except socket.gaierror as e:
        return False, None, None, f"DNS resolution failed: {e}"
    except TimeoutError:
        return False, None, None, "DNS resolution timeout"
    except (OSError, UnicodeError) as e:
        # Any other resolver failure (socket error, bad IDNA hostname) is
        # still a DNS failure.
        return False, None, None, str(e)


def connect_tcp(ip: str, port: str, timeout: float = 8.0) -> tuple[bool, socket.socket | None, float | None, str | None]:
    """Establish TCP connection to IP address.

    Args:
        ip: Target IP address
        port: Target port as string
        timeout: Connection timeout in seconds

    Returns:
        Tuple of (success, connected_socket, elapsed_ms, error_message)
        On success, returns a connected socket that the caller must close.
    """
    try:
        start_time = time.perf_counter()

        # Create socket with timeout
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((ip, int(port)))

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        return True, sock, elapsed_ms, None
    except TimeoutError:
        return False, None, None, "Connection timeout"
    except ConnectionRefusedError:
        return False, None, None, "Connection refused"
    except OSError as e:
        if "Connection refused" in str(e):
            return False, None, None, "Connection refused"
        return False, None, None, f"Connection failed: {e}"
    except ValueError as e:
        # Invalid port number.
        return False, None, None, f"Invalid port: {e}"


def check_certificate(
    expiry_date: str | None,
    warning_days: int = 7,
) -> tuple[bool, bool, datetime | None]:
    """Check certificate expiry and compute how long the certificate has left.

    The 'notAfter' value is parsed with ssl.cert_time_to_seconds(), which
    handles the OpenSSL format ("May 26 23:59:59 2024 GMT") including the
    double space used for single digit days.

    Args:
        expiry_date: The 'notAfter' value from the certificate
        warning_days: Days before expiry at which the certificate is flagged
            as expiring soon (configurable, not hardcoded)

    Returns:
        Tuple of (is_expired, is_expiring_soon, cert_expires_at)
        cert_expires_at is a timezone-aware datetime, or None if unknown.
    """
    if not expiry_date:
        # Can't verify cert without expiry date
        return False, False, None

    try:
        expires_epoch = ssl.cert_time_to_seconds(expiry_date)
        cert_expires_at = datetime.fromtimestamp(expires_epoch, tz=UTC)

        seconds_remaining = expires_epoch - time.time()
        days_until_expiry = seconds_remaining / 86400

        if days_until_expiry < 0:
            return True, False, cert_expires_at
        if days_until_expiry <= warning_days:
            return False, True, cert_expires_at

        return False, False, cert_expires_at
    except (ValueError, OverflowError, OSError) as e:
        logger.debug(f"Failed to parse cert expiry date: {e}")
        return False, False, None


def do_tls(tcp_sock: socket.socket, hostname: str, timeout: float = 10.0,
          warning_days: int = 7) -> tuple[bool, ssl.SSLSocket | None, float | None, str | None,
                                         dict[str, Any] | None, datetime | None, bool, bool, bool]:
    """Perform TLS handshake on an existing TCP socket.

    Args:
        tcp_sock: Already connected TCP socket
        hostname: Hostname for SNI and certificate validation
        timeout: TLS handshake timeout in seconds
        warning_days: Days before expiry at which the certificate is flagged
            as expiring soon (configurable via CERT_EXPIRY_WARNING_DAYS env var)

    Returns:
        Tuple of (success, ssl_socket, elapsed_ms, error_message, cert_info,
                  cert_expires_at, cert_expired, cert_expiring_soon, cert_invalid)
        On success, returns an SSL-wrapped socket that the caller must close.
        cert_expires_at is a timezone-aware datetime, or None if unknown.
    """
    try:
        start_time = time.perf_counter()

        # Create SSL context that verifies certificates
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED

        # Wrap the existing TCP socket with SSL
        ssl_sock = context.wrap_socket(tcp_sock, server_hostname=hostname)

        # Get certificate (after handshake)
        cert = ssl_sock.getpeercert(binary_form=False)
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        # Extract certificate info
        cert_info = None
        cert_expires_at = None
        cert_expired = False
        cert_expiring_soon = False
        cert_invalid = False

        if cert:
            # Get raw certificate for expiry check
            not_after = cert.get('notAfter')
            if not_after:
                cert_expired, cert_expiring_soon, cert_expires_at = check_certificate(
                    not_after, warning_days
                )

            cert_info = {
                "subject": dict(x[0] for x in cert.get('subject', [])) if cert.get('subject') else {},
                "issuer": dict(x[0] for x in cert.get('issuer', [])) if cert.get('issuer') else {},
                "version": cert.get('version'),
                "notBefore": cert.get('notBefore'),
                "notAfter": not_after,
                "subjectAltName": cert.get('subjectAltName', []),
            }

        return True, ssl_sock, elapsed_ms, None, cert_info, cert_expires_at, cert_expired, cert_expiring_soon, cert_invalid
    except ssl.SSLCertVerificationError as e:
        # Certificate verification failed. verify_code tells us whether the
        # certificate itself is expired (CERT_EXPIRED) or the failure is some
        # other trust problem (CERT_INVALID, e.g. hostname mismatch).
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        verify_code = getattr(e, "verify_code", None)
        cert_expired = verify_code in (
            X509_V_ERR_CERT_NOT_YET_VALID,
            X509_V_ERR_CERT_HAS_EXPIRED,
        )
        cert_invalid = not cert_expired
        # Expiry flags are unknown here: the handshake failed before we could
        # read the certificate's notAfter date.
        cert_expiring_soon = False
        cert_expires_at = None

        return False, None, elapsed_ms, f"Certificate error: {e}", None, cert_expires_at, cert_expired, cert_expiring_soon, cert_invalid
    except ssl.SSLError as e:
        return False, None, None, f"TLS error: {e}", None, None, False, False, False
    except TimeoutError:
        return False, None, None, "TLS handshake timeout", None, None, False, False, False
    except (OSError, ValueError) as e:
        # ssl.SSLError is an OSError subclass and is handled above; anything
        # else here is a socket-level failure during the handshake.
        return False, None, None, str(e), None, None, False, False, False


def do_http(sock: socket.socket | ssl.SSLSocket, path: str, hostname: str,
            timeout: float = 15.0, max_body_size: int = 1048576) -> tuple[bool, int | None, int | None, float | None, float | None, str | None, str | None]:
    """Send HTTP request and get response using an existing socket.

    Args:
        sock: Already connected TCP or SSL socket
        path: Request path (with leading /)
        hostname: Hostname for Host header
        timeout: Read timeout in seconds
        max_body_size: Maximum response body size to read (default 1 MB)

    Returns:
        Tuple of (success, status_code, response_size, ttfb_ms, total_ms,
                  response_body, error_message)
    """
    start_time = time.perf_counter()
    try:
        # The socket inherits the connect timeout from connect_tcp; the read
        # timeout of this stage is applied here so that a hung server is
        # reported as a read timeout.
        sock.settimeout(timeout)

        # Send HTTP request
        request = f"GET {path} HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n"
        sock.sendall(request.encode('utf-8'))

        # Use makefile to read the response properly
        f = sock.makefile('rb')
        try:
            # Read status line. The first byte of the response arrives here,
            # so this is the real time to first byte (TTFB).
            status_line = f.readline().decode('utf-8', errors='replace').strip()
            ttfb_ms = (time.perf_counter() - start_time) * 1000

            if not status_line:
                return False, None, None, None, None, None, "No response received"

            # Parse status line
            parts = status_line.split(' ', 2)
            if len(parts) < 2:
                return False, None, None, None, None, None, f"Invalid response: {status_line}"

            status_code = int(parts[1]) if parts[1].isdigit() else None

            # Read headers until blank line
            while True:
                line = f.readline().decode('utf-8', errors='replace')
                if line.strip() == '' or not line:
                    break

            # Read response body, limited to max_body_size + 1 to detect truncation
            body = f.read(max_body_size + 1)
            total_ms = (time.perf_counter() - start_time) * 1000

            response_size = len(body)
            if response_size > max_body_size:
                body = body[:max_body_size]
                response_size = len(body)

            response_body = body.decode('utf-8', errors='replace')

            return True, status_code, response_size, ttfb_ms, total_ms, response_body, None
        finally:
            # Closing the makefile also closes the underlying socket.
            with suppress(OSError):
                f.close()
    except TimeoutError:
        total_ms = (time.perf_counter() - start_time) * 1000
        return False, None, None, None, total_ms, None, "HTTP read timeout"
    except (OSError, ValueError) as e:
        return False, None, None, None, None, None, str(e)


def check_url(
    url: str,
    slow_threshold_ms: int = 5000,
    cert_expiry_warning_days: int = 7,
    expected_status: int = 200,
    timeouts: NetworkTimeouts | None = None,
    expected_keyword: str | None = None,
) -> CheckResult:
    """Perform a complete URL check with all stages.

    This is the public entry point and it never raises: an unexpected error is
    caught and reported as an INTERNAL_ERROR result, so that a single bad
    site can never stop a check run (spec section 10).

    Args:
        url: The URL to check (http or https)
        slow_threshold_ms: Threshold for slow status (milliseconds)
        cert_expiry_warning_days: Days before expiry to trigger the
            cert_expiring_soon flag (configurable, not hardcoded)
        expected_status: HTTP status code the site is expected to return
            (default 200). A matching 3xx is OK, not an error.
        timeouts: Network timeout configuration
        expected_keyword: Optional keyword to search in response body

    Returns:
        CheckResult with all stage timings and classification
    """
    start = time.perf_counter()
    try:
        return _run_check(
            url,
            slow_threshold_ms=slow_threshold_ms,
            cert_expiry_warning_days=cert_expiry_warning_days,
            expected_status=expected_status,
            timeouts=timeouts,
            expected_keyword=expected_keyword,
        )
    except Exception as exc:
        # A single bad site must never break the run: the error is logged and
        # reported as a result instead of propagating.
        logger.exception("Unexpected error while checking %s", url)
        result = CheckResult()
        result.result = "INTERNAL_ERROR"
        result.failed_stage = "unknown"
        result.error_message = f"Unexpected error: {exc}"
        result.total_ms = (time.perf_counter() - start) * 1000
        return result


def _run_check(
    url: str,
    slow_threshold_ms: int = 5000,
    cert_expiry_warning_days: int = 7,
    expected_status: int = 200,
    timeouts: NetworkTimeouts | None = None,
    expected_keyword: str | None = None,
) -> CheckResult:
    """Run the DNS -> TCP -> TLS -> HTTP stages for a single URL.

    Uses a single connection through all stages:
    resolve DNS -> connect TCP -> (HTTPS: wrap with TLS) -> send HTTP request
    over the same socket (TLS socket for HTTPS).

    See check_url() for the arguments and the returned CheckResult.
    """
    if timeouts is None:
        timeouts = NetworkTimeouts()

    result = CheckResult()
    overall_start = time.perf_counter()

    # Step 1: Parse URL
    success, scheme, hostname, port, path, error_msg, _ = parse_url(url)
    if not success:
        result.error_message = error_msg
        result.failed_stage = "url_parse"
        result.status_code = 400
        result.total_ms = (time.perf_counter() - overall_start) * 1000
        result.result = "HTTP_4XX"  # Treat invalid URLs as 400 errors
        return result

    # Step 2: DNS Resolution
    dns_success, ip, dns_ms, dns_error = resolve_dns(hostname, timeouts.dns_timeout)
    result.dns_ms = dns_ms

    if not dns_success:
        result.dns_success = False
        result.error_message = dns_error
        result.failed_stage = "dns"
        result.total_ms = (time.perf_counter() - overall_start) * 1000
        result.result, result.failed_stage = classify_result(
            dns_success=False, tcp_success=False, tls_success=False, http_success=False,
            status_code=None, ttfb_ms=None, total_ms=None, slow_threshold_ms=slow_threshold_ms,
            keyword_found=None, expected_status=expected_status
        )
        return result

    result.dns_success = True
    result.resolved_ip = ip

    # SSRF check - block private/loopback/link-local IPs after DNS resolution.
    # A blocked target gets its own code so it is not reported as a DNS failure.
    if is_private_ip(ip):
        result.dns_success = False
        result.error_message = f"SSRF blocked: {ip} is a private/loopback address"
        result.failed_stage = "dns"
        result.total_ms = (time.perf_counter() - overall_start) * 1000
        result.result = "BLOCKED_TARGET"
        return result

    # Step 3: TCP Connection
    tcp_success, tcp_sock, tcp_ms, tcp_error = connect_tcp(
        ip, port, timeouts.connect_timeout
    )

    if not tcp_success:
        result.tcp_success = False
        result.tcp_ms = tcp_ms
        result.error_message = tcp_error
        result.failed_stage = "tcp"
        result.total_ms = (time.perf_counter() - overall_start) * 1000
        result.result, result.failed_stage = classify_result(
            dns_success=True, tcp_success=False, tls_success=False, http_success=False,
            status_code=None, ttfb_ms=None, total_ms=None, slow_threshold_ms=slow_threshold_ms,
            keyword_found=None, expected_status=expected_status, tcp_error=tcp_error
        )
        return result

    result.tcp_success = True
    result.tcp_ms = tcp_ms

    # Step 4: TLS Handshake (for HTTPS)
    sock = tcp_sock
    if scheme == "https":
        tls_success, ssl_sock, tls_ms, tls_error, cert_info, cert_expires_at, cert_expired, cert_expiring_soon, cert_invalid = do_tls(
            tcp_sock, hostname, timeouts.tls_timeout, warning_days=cert_expiry_warning_days
        )
        result.tls_success = tls_success
        result.tls_ms = tls_ms
        result.cert_info = cert_info
        result.cert_expires_at = cert_expires_at
        result.cert_expired = cert_expired
        result.cert_expiring_soon = cert_expiring_soon
        result.cert_invalid = cert_invalid

        if not tls_success:
            result.error_message = tls_error
            result.failed_stage = "tls"
            result.total_ms = (time.perf_counter() - overall_start) * 1000

            # The handshake failed, so no SSL socket exists: close the plain
            # TCP socket instead of leaking it.
            with suppress(OSError):
                tcp_sock.close()

            # Determine specific TLS error. CERT_EXPIRING is no longer a
            # result code: it is a flag (cert_expiring_soon) surfaced later.
            if cert_expired:
                result.result = "CERT_EXPIRED"
            elif cert_invalid:
                result.result = "CERT_INVALID"
            else:
                result.result = "TLS_ERROR"

            return result

        sock = ssl_sock
    else:
        # HTTP (not HTTPS) - no TLS
        result.tls_success = True  # N/A but mark as success for classification
        result.tls_ms = 0

    # Step 5: HTTP Request
    http_success, status_code, response_size, ttfb_ms, _http_total_ms, response_body, http_error = do_http(
        sock, path, hostname, timeouts.read_timeout
    )

    result.http_success = http_success
    result.status_code = status_code
    result.response_size = response_size
    result.ttfb_ms = ttfb_ms
    # total_ms is the time of the whole check (DNS + TCP + TLS + HTTP), not
    # just the HTTP stage: this is what the site owner experiences and what
    # the slow threshold is compared against.
    result.total_ms = (time.perf_counter() - overall_start) * 1000

    if not http_success:
        result.error_message = http_error
        result.failed_stage = "http"

        # A dropped/closed connection is a connection-level failure, not an
        # HTTP error: it must not be reported as HTTP_5XX.
        result.result = "READ_TIMEOUT"

        # Clean up socket
        with suppress(OSError):
            sock.close()

        return result

    # Check keyword if provided
    if expected_keyword:
        result.keyword_found = expected_keyword in (response_body or "")

    # Classify the result
    result.result, result.failed_stage = classify_result(
        dns_success=True,
        tcp_success=True,
        tls_success=result.tls_success,
        http_success=True,
        status_code=status_code,
        ttfb_ms=ttfb_ms,
        total_ms=result.total_ms,
        slow_threshold_ms=slow_threshold_ms,
        keyword_found=result.keyword_found,
        expected_status=expected_status,
        cert_expired=result.cert_expired,
        cert_expiring_soon=result.cert_expiring_soon,
        cert_invalid=result.cert_invalid,
        dns_ms=result.dns_ms,
    )

    # Clean up socket
    with suppress(OSError):
        sock.close()

    return result