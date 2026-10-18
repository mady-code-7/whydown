"""Network checking functions for WhyDown.

Implements the stage functions from section 10 of the spec:
- DNS resolution (resolve_dns)
- TCP connection (connect_tcp)
- TLS handshake (do_tls)
- HTTP request/response (do_http)

All functions are pure and can be unit-tested with mocks.
"""

from __future__ import annotations

import http.client
import logging
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import UTC
from typing import Any
from urllib.parse import urlparse

from .classifier import classify_result

logger = logging.getLogger(__name__)


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
    total_ms: float | None = None

    # HTTP data
    status_code: int | None = None
    response_size: int | None = None
    response_body: str | None = None

    # IP and certificate info
    resolved_ip: str | None = None
    cert_info: dict[str, Any] | None = None

    # Error information
    error_message: str | None = None
    failed_stage: str | None = None

    # Classification result
    result: str = "OK"
    keyword_found: bool | None = None

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
    except Exception as e:
        return False, None, None, None, None, str(e), None


def is_private_ip(ip: str) -> bool:
    """Check if an IP address is private, loopback, or link-local (SSRF protection)."""
    try:
        addr = socket.inet_aton(ip)

        # Check loopback (127.0.0.0/8)
        if addr[0] == 127:
            return True

        # Check private ranges
        # 10.0.0.0/8
        if addr[0] == 10:
            return True

        # 172.16.0.0/12
        if addr[0] == 172 and 16 <= addr[1] <= 31:
            return True

        # 192.168.0.0/16
        if addr[0] == 192 and addr[1] == 168:
            return True

        # 169.254.0.0/16 (link-local)
        if addr[0] == 169 and addr[1] == 254:
            return True

        # Check multicast (224.0.0.0/4)
        return addr[0] in range(224, 240)
    except Exception:
        return True  # Block on error


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
    except Exception as e:
        return False, None, None, str(e)


def connect_tcp(ip: str, port: str, timeout: float = 8.0) -> tuple[bool, float | None, str | None]:
    """Establish TCP connection to IP address.

    Args:
        ip: Target IP address
        port: Target port as string
        timeout: Connection timeout in seconds

    Returns:
        Tuple of (success, elapsed_ms, error_message)
    """
    try:
        start_time = time.perf_counter()

        # Create socket with timeout
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((ip, int(port)))

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        sock.close()

        return True, elapsed_ms, None
    except TimeoutError:
        return False, None, "Connection timeout"
    except ConnectionRefusedError:
        return False, None, "Connection refused"
    except OSError as e:
        if "Connection refused" in str(e):
            return False, None, "Connection refused"
        return False, None, f"Connection failed: {e}"
    except Exception as e:
        return False, None, str(e)


def check_certificate(expiry_date: str | None, hostname: str) -> tuple[bool, bool, bool]:
    """Check certificate validity.

    Args:
        expiry_date: The 'notAfter' value from certificate
        hostname: The hostname to match against

    Returns:
        Tuple of (is_expired, is_expiring_soon, is_invalid)
    """
    is_expired = False
    is_expiring_soon = False
    is_invalid = False

    if not expiry_date:
        # Can't verify cert without expiry date
        return is_expired, is_expiring_soon, is_invalid

    try:
        # Parse certificate expiry date
        # OpenSSL format: "May 26 23:59:59 2024 GMT"
        # or "Aug  7 12:00:00 2024 GMT" (note extra space for day < 10)
        from datetime import datetime

        # Normalize the date string
        expiry_date = expiry_date.strip()
        if len(expiry_date.split()[0]) == 1:  # Single digit day
            expiry_date = expiry_date.replace(expiry_date.split()[0],
                                               "0" + expiry_date.split()[0], 1)

        # Try parsing
        cert_expiry = datetime.strptime(expiry_date, "%b %d %H:%M:%S %Y %Z")
        cert_expiry = cert_expiry.replace(tzinfo=UTC)

        now = datetime.now(UTC)
        days_until_expiry = (cert_expiry - now).days

        if days_until_expiry < 0:
            is_expired = True
        elif days_until_expiry <= 7:  # CERT_EXPIRY_WARNING_DAYS
            is_expiring_soon = True
    except Exception as e:
        logger.debug(f"Failed to parse cert expiry date: {e}")

    return is_expired, is_expiring_soon, is_invalid


def do_tls(ip: str, port: str, hostname: str, timeout: float = 10.0) -> tuple[bool, float | None, str | None, dict[str, Any] | None, bool, bool, bool]:
    """Perform TLS handshake and get certificate info.

    Args:
        ip: Target IP address
        port: Target port as string
        hostname: Hostname for SNI and certificate validation
        timeout: TLS handshake timeout in seconds

    Returns:
        Tuple of (success, elapsed_ms, error_message, cert_info, cert_expired, cert_expiring_soon, cert_invalid)
    """
    try:
        start_time = time.perf_counter()

        # Create SSL context that verifies certificates
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED

        # Create socket and wrap with SSL
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)

        ssl_sock = context.wrap_socket(sock, server_hostname=hostname)
        ssl_sock.connect((ip, int(port)))

        # Get certificate
        cert = ssl_sock.getpeercert(binary_form=False)
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        ssl_sock.close()

        # Extract certificate info
        cert_info = None
        cert_expired = False
        cert_expiring_soon = False
        cert_invalid = False

        if cert:
            # Get raw certificate for expiry check
            # The getpeercert() without binary_form already parsed it
            # We need to check the expiry date
            not_after = cert.get('notAfter')
            if not_after:
                cert_expired, cert_expiring_soon, cert_invalid = check_certificate(not_after, hostname)

            cert_info = {
                "subject": dict(x[0] for x in cert.get('subject', [])) if cert.get('subject') else {},
                "issuer": dict(x[0] for x in cert.get('issuer', [])) if cert.get('issuer') else {},
                "version": cert.get('version'),
                "notBefore": cert.get('notBefore'),
                "notAfter": not_after,
                "subjectAltName": cert.get('subjectAltName', []),
            }

        return True, elapsed_ms, None, cert_info, cert_expired, cert_expiring_soon, cert_invalid
    except ssl.SSLCertVerificationError as e:
        # Certificate verification failed
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        # Check if it's expired or just hostname mismatch
        cert_expired, cert_expiring_soon, cert_invalid = False, False, False
        err_msg = str(e)

        if "expired" in err_msg.lower():
            cert_expired = True
        elif "hostname" in err_msg.lower() or "name match" in err_msg.lower():
            cert_invalid = True
        else:
            cert_invalid = True

        return False, elapsed_ms, f"Certificate error: {e}", None, cert_expired, cert_expiring_soon, cert_invalid
    except ssl.SSLError as e:
        return False, None, f"TLS error: {e}", None, False, False, False
    except TimeoutError:
        return False, None, "TLS handshake timeout", None, False, False, False
    except Exception as e:
        return False, None, str(e), None, False, False, False


def do_http(ip: str, port: str, path: str, hostname: str,
            timeout: float = 15.0) -> tuple[bool, int | None, int | None, float | None, float | None, str | None, str | None]:
    """Send HTTP request and get response.

    Args:
        ip: Target IP address
        port: Target port as string
        path: Request path (with leading /)
        hostname: Hostname for Host header
        timeout: Read timeout in seconds

    Returns:
        Tuple of (success, status_code, response_size, ttfb_ms, total_ms, response_body, error_message)
    """
    try:
        start_time = time.perf_counter()

        # Create HTTP connection
        conn = http.client.HTTPConnection(ip, int(port), timeout=timeout)

        # Send request with Host header
        headers = {"Host": hostname}
        conn.request("GET", path, headers=headers)

        # Get response
        response = conn.getresponse()
        ttfb_ms = (time.perf_counter() - start_time) * 1000

        # Read response body
        body = response.read()
        total_ms = (time.perf_counter() - start_time) * 1000

        response_size = len(body)
        response_body = body.decode('utf-8', errors='replace')

        status_code = response.status

        conn.close()

        return True, status_code, response_size, ttfb_ms, total_ms, response_body, None
    except TimeoutError:
        total_ms = (time.perf_counter() - start_time) * 1000
        return False, None, None, None, total_ms, None, "HTTP read timeout"
    except http.client.HTTPException as e:
        return False, None, None, None, None, None, f"HTTP error: {e}"
    except Exception as e:
        return False, None, None, None, None, None, str(e)


def check_url(
    url: str,
    slow_threshold_ms: int = 5000,
    cert_expiry_warning_days: int = 7,
    timeouts: NetworkTimeouts | None = None,
    expected_keyword: str | None = None,
) -> CheckResult:
    """Perform a complete URL check with all stages.

    Args:
        url: The URL to check (http or https)
        slow_threshold_ms: Threshold for slow status (milliseconds)
        cert_expiry_warning_days: Days before expiry to trigger warning
        timeouts: Network timeout configuration
        expected_keyword: Optional keyword to search in response body

    Returns:
        CheckResult with all stage timings and classification
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
        # Classification based on DNS failure
        result.result, result.failed_stage = classify_result(
            dns_success=False, tcp_success=False, tls_success=False, http_success=False,
            status_code=None, ttfb_ms=None, total_ms=None, slow_threshold_ms=slow_threshold_ms,
            keyword_found=None
        )
        return result

    result.dns_success = True
    result.resolved_ip = ip

    # SSRF check - block private IPs
    if is_private_ip(ip):
        result.dns_success = False
        result.error_message = f"SSRF blocked: {ip} is a private/loopback address"
        result.failed_stage = "dns"
        result.total_ms = (time.perf_counter() - overall_start) * 1000
        result.result = "DNS_FAILURE"
        return result

    # Step 3: TCP Connection
    tcp_start = time.perf_counter()
    try:
        tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp_sock.settimeout(timeouts.connect_timeout)
        tcp_sock.connect((ip, int(port)))
        tcp_sock.close()

        tcp_ms = (time.perf_counter() - tcp_start) * 1000
        result.tcp_success = True
        result.tcp_ms = tcp_ms
    except TimeoutError:
        result.tcp_success = False
        result.tcp_ms = timeouts.connect_timeout * 1000
        result.error_message = "TCP connection timeout"
        result.failed_stage = "tcp"
        result.total_ms = (time.perf_counter() - overall_start) * 1000
        result.result, result.failed_stage = classify_result(
            dns_success=True, tcp_success=False, tls_success=False, http_success=False,
            status_code=None, ttfb_ms=None, total_ms=None, slow_threshold_ms=slow_threshold_ms,
            keyword_found=None
        )
        return result
    except ConnectionRefusedError:
        result.tcp_success = False
        result.tcp_ms = (time.perf_counter() - tcp_start) * 1000
        result.error_message = "Connection refused"
        result.failed_stage = "tcp"
        result.total_ms = (time.perf_counter() - overall_start) * 1000
        result.result, result.failed_stage = classify_result(
            dns_success=True, tcp_success=False, tls_success=False, http_success=False,
            status_code=None, ttfb_ms=None, total_ms=None, slow_threshold_ms=slow_threshold_ms,
            keyword_found=None
        )
        return result
    except Exception as e:
        result.tcp_success = False
        result.tcp_ms = (time.perf_counter() - tcp_start) * 1000
        result.error_message = str(e)
        result.failed_stage = "tcp"
        result.total_ms = (time.perf_counter() - overall_start) * 1000
        result.result, result.failed_stage = classify_result(
            dns_success=True, tcp_success=False, tls_success=False, http_success=False,
            status_code=None, ttfb_ms=None, total_ms=None, slow_threshold_ms=slow_threshold_ms,
            keyword_found=None
        )
        return result

    # Step 4: TLS Handshake (for HTTPS)
    if scheme == "https":
        time.perf_counter()
        tls_success, tls_ms, tls_error, cert_info, cert_expired, cert_expiring_soon, cert_invalid = do_tls(
            ip, port, hostname, timeouts.tls_timeout
        )
        result.tls_success = tls_success
        result.tls_ms = tls_ms
        result.cert_info = cert_info
        result.cert_expired = cert_expired
        result.cert_expiring_soon = cert_expiring_soon
        result.cert_invalid = cert_invalid

        if not tls_success:
            result.error_message = tls_error
            result.failed_stage = "tls"
            result.total_ms = (time.perf_counter() - overall_start) * 1000

            # Determine specific TLS error
            if cert_expired:
                result.result = "CERT_EXPIRED"
            elif cert_invalid:
                result.result = "CERT_INVALID"
            elif cert_expiring_soon:
                result.result = "CERT_EXPIRING"
            else:
                result.result = "TLS_ERROR"

            return result
    else:
        # HTTP (not HTTPS) - no TLS
        result.tls_success = True  # N/A but mark as success for classification
        result.tls_ms = 0

    # Step 5: HTTP Request
    time.perf_counter()
    http_success, status_code, response_size, ttfb_ms, total_ms, response_body, http_error = do_http(
        ip, port, path, hostname, timeouts.read_timeout
    )

    result.http_success = http_success
    result.status_code = status_code
    result.response_size = response_size
    result.response_body = response_body
    result.ttfb_ms = ttfb_ms
    result.total_ms = total_ms

    if not http_success:
        result.error_message = http_error
        result.failed_stage = "http"

        if "timeout" in str(http_error).lower() or http_error is None:
            result.result = "READ_TIMEOUT"
        else:
            result.result = "HTTP_5XX"

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
        total_ms=total_ms,
        slow_threshold_ms=slow_threshold_ms,
        keyword_found=result.keyword_found,
        cert_expired=result.cert_expired,
        cert_expiring_soon=result.cert_expiring_soon,
        cert_invalid=result.cert_invalid
    )

    return result