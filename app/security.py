"""URL validation and SSRF filter for WhyDown.

Implements the security considerations from spec section 12:
- Block private/loopback/link-local addresses after DNS resolution
- Allow only http/https schemes
- Limit sites, response size, redirects, and timeouts
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import urllib.parse
from collections.abc import Callable


class SecurityError(ValueError):
    """Raised when a URL target fails the SSRF safety check."""


def is_private_ip(ip: str) -> bool:
    """Check if an IP address must never be contacted (SSRF protection).

    Blocks every address that is not globally routable (private, loopback,
    link-local, shared address space, ...) using the ipaddress module's
    is_global property. On top of that the ranges that is_global counts as
    global on some Python versions are blocked by name as well: multicast
    (224.0.0.0/4), reserved (240.0.0.0/4, which includes the broadcast
    address) and the unspecified address (0.0.0.0).

    An address that cannot be parsed is blocked as well: the safe answer to
    "is this address safe?" is "no" when the address is not an address.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        # Invalid IP -> block
        return True

    if addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        return True

    return not addr.is_global


def _is_local_hostname(hostname: str) -> bool:
    """Return True if the hostname is a local-only name."""
    lower = hostname.lower()
    return (
        lower == "localhost"
        or lower.endswith((".localhost", ".local", ".localdomain", ".internal"))
    )


def _try_parse_ip_literal(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Try to interpret hostname as an IP literal.

    Handles standard IPv4, standard IPv6, IPv4-mapped IPv6 addresses
    (e.g. ::ffff:127.0.0.1), and decimal or hex IP forms like
    '2130706433' or '0x7f000001'.

    Returns the parsed address, or None if the hostname is not an IP literal.
    """
    # Standard IP literal (handles IPv4, IPv6, and IPv4-mapped IPv6)
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        pass

    # Decimal or hex IP form (e.g., 2130706433, 0x7f000001)
    try:
        as_int = int(hostname, 0)  # auto-detects base
        if 0 <= as_int <= 0xFFFFFFFF:
            return ipaddress.IPv4Address(as_int)
    except (ValueError, TypeError):
        pass

    return None


_DNS_TIMEOUT_SECONDS = 5.0


def _resolve_with_timeout(
    hostname: str,
    resolver: Callable | None = None,
    timeout: float = _DNS_TIMEOUT_SECONDS,
) -> list[tuple[int, int, int, str, tuple[str, ...]]]:
    """Run DNS resolution in a thread with a bounded time."""
    if resolver is None:
        resolver = socket.getaddrinfo

    result: dict = {}

    def _do_lookup() -> None:
        try:
            result["addrs"] = resolver(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except OSError as exc:
            result["error"] = exc

    t = threading.Thread(target=_do_lookup, daemon=True)
    t.start()
    t.join(timeout)

    if t.is_alive():
        raise socket.gaierror(socket.EAI_AGAIN, "DNS resolution timed out")

    if isinstance(result.get("error"), BaseException):
        raise result["error"]

    return result.get("addrs", [])


def check_url_target(url: str, resolver: Callable | None = None) -> list[str]:
    """Resolve a URL's target and verify it is safe from SSRF attacks.

    Raises SecurityError if the target is unsafe. The check covers:
    - Local hostnames (localhost, *.localhost, *.local, *.internal)
    - IP literals that are private/loopback/link-local/multicast/reserved/unspecified
    - DNS-resolved addresses that are private/loopback/link-local/multicast/reserved/unspecified
    - A mix of public and private addresses (any private address causes rejection)
    - IPv4-mapped IPv6 addresses (e.g., ::ffff:127.0.0.1)
    - Decimal or hex IP forms (e.g., http://2130706433/)

    Args:
        url: The URL to check (must have http or https scheme).
        resolver: Optional callable for DNS resolution (used for tests).
            Defaults to socket.getaddrinfo.

    Returns:
        List of resolved IP addresses (for informational purposes).

    Raises:
        SecurityError: If the target is unsafe.
    """
    # Parse URL
    try:
        parsed = urllib.parse.urlparse(url)
    except (ValueError, TypeError) as exc:
        raise SecurityError(f"Could not parse URL: {exc}") from exc

    hostname = parsed.hostname
    if not hostname:
        raise SecurityError("URL has no hostname")

    # Check for local hostnames
    if _is_local_hostname(hostname):
        raise SecurityError(f"Hostname '{hostname}' refers to the local machine")

    # Check if it's an IP literal
    addr = _try_parse_ip_literal(hostname)
    if addr is not None:
        if is_private_ip(str(addr)):
            raise SecurityError(f"IP address {addr} is private or non-routable")
        return [str(addr)]

    # It's a hostname - resolve via DNS with bounded time
    try:
        addrs = _resolve_with_timeout(hostname, resolver)
    except socket.gaierror as exc:
        raise SecurityError(f"DNS resolution failed for '{hostname}': {exc}") from exc

    if not addrs:
        raise SecurityError(f"DNS resolution returned no addresses for '{hostname}'")

    # Check all resolved IPs
    resolved_ips: list[str] = []
    for family, type_, proto, canonname, sockaddr in addrs:
        ip = sockaddr[0]
        resolved_ips.append(ip)
        if is_private_ip(ip):
            raise SecurityError(f"Resolved address {ip} for '{hostname}' is private or non-routable")

    return resolved_ips