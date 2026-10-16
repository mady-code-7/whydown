"""Network checking functions for WhyDown.

Implements the stage functions from section 10 of the spec:
- DNS resolution (resolve_dns)
- TCP connection (connect_tcp)
- TLS handshake (do_tls)
- HTTP request/response (do_http)
- Classification (classify)

All functions are pure and can be unit-tested with mocks.
"""

import socket
import ssl
import http.client
import time
from urllib.parse import urlparse
from typing import Tuple, Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


def resolve_dns(hostname: str, timeout: int = 5) -> Tuple[bool, Optional[str], Optional[float], Optional[str]]:
    """Resolve hostname to IP address.

    Args:
        hostname: The hostname to resolve
        timeout: DNS resolution timeout in seconds

    Returns:
        Tuple of (success, ip_address, elapsed_ms, error_message)
        - success: True if DNS resolution succeeded
        - ip_address: Resolved IP address or None
        - elapsed_ms: Resolution time in milliseconds or None
        - error_message: Error message if failed, None if success
    """
    try:
        start_time = time.perf_counter()
        resolved_ip = socket.gethostbyname(hostname)
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        return True, resolved_ip, elapsed_ms, None
    except Exception as e:
        logger.debug(f"DNS resolution failed for {hostname}: {e}")
        return False, None, None, str(e)


def connect_tcp(ip: str, port: int, timeout: int = 8) -> Tuple[bool, Optional[float], Optional[str]]:
    """Establish TCP connection to IP address.

    Args:
        ip: Target IP address
        port: Target port
        timeout: Connection timeout in seconds

    Returns:
        Tuple of (success, elapsed_ms, error_message)
    """
    try:
        start_time = time.perf_counter()

        # Create socket with timeout
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((ip, port))

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        sock.close()

        return True, elapsed_ms, None
    except Exception as e:
        logger.debug(f"TCP connection failed to {ip}:{port}: {e}")
        return False, None, str(e)


def do_tls(ip: str, port: int, hostname: str, timeout: int = 10) -> Tuple[bool, Optional[float], Optional[str], Optional[Dict[str, Any]]]:
    """Perform TLS handshake and get certificate info.

    Args:
        ip: Target IP address
        port: Target port
        hostname: Hostname for SNI and certificate validation
        timeout: TLS handshake timeout in seconds

    Returns:
        Tuple of (success, elapsed_ms, error_message, cert_info)
        - cert_info: dict with cert details if successful, None if failed
    """
    try:
        start_time = time.perf_counter()

        # Create SSL context
        context = ssl.create_default_context()
        context.check_hostname = False  # We'll validate manually in security module
        context.verify_mode = ssl.CERT_NONE

        # Create socket and wrap with SSL
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        ssl_sock = context.wrap_socket(sock, server_hostname=hostname)
        ssl_sock.connect((ip, port))

        # Get certificate
        cert = ssl_sock.getpeercert()
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        ssl_sock.close()

        # Extract certificate info
        cert_info = None
        if cert:
            cert_info = {
                "subject": dict(cert.get('subject', [])),
                "issuer": dict(cert.get('issuer', [])),
                "version": cert.get('version'),
                "serial_number": cert.get('serialNumber'),
                "notBefore": cert.get('notBefore'),
                "notAfter": cert.get('notAfter'),
                "subjectAltName": cert.get('subjectAltName', []),
            }

        return True, elapsed_ms, None, cert_info
    except Exception as e:
        logger.debug(f"TLS handshake failed to {ip}:{port}: {e}")
        return False, None, str(e), None


def do_http(ip: str, port: int, path: str = "/", method: str = "GET",
          headers: Optional[Dict[str, str]] = None, hostname: str = "localhost",
          timeout: int = 15) -> Tuple[bool, Optional[int], Optional[int], Optional[float], Optional[float], Optional[str], Optional[str]]:
    """Send HTTP request and get response.

    Args:
        ip: Target IP address
        port: Target port
        path: Request path (without leading /)
        method: HTTP method
        headers: Optional request headers
        hostname: Hostname for Host header
        timeout: Read timeout in seconds

    Returns:
        Tuple of (success, status_code, response_size, ttfb_ms, total_ms, error_message, response_body)
        - status_code: HTTP status code or None if failed
        - response_size: Response size in bytes or None if failed
        - ttfb_ms: Time to first byte in milliseconds or None if failed
        - total_ms: Total request time in milliseconds or None if failed
        - response_body: Response body as string or None if failed
    """
    if headers is None:
        headers = {}

    if path.startswith("/"):
        path = path[1:]

    try:
        start_time = time.perf_counter()

        # Create connection
        conn = http.client.HTTPConnection(ip, port, timeout=timeout)
        conn.request(method, "/" + path, "", headers)

        # Get response
        response = conn.getresponse()
        ttfb_ms = (time.perf_counter() - start_time) * 1000

        # Read response body
        response_body = response.read().decode('utf-8', errors='replace')
        total_ms = (time.perf_counter() - start_time) * 1000

        conn.close()

        return True, response.status, len(response_body), ttfb_ms, total_ms, None, response_body
    except Exception as e:
        logger.debug(f"HTTP request failed to {ip}:{port}/{path}: {e}")
        return False, None, None, None, None, str(e), None