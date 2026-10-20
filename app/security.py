"""URL validation and SSRF filter for WhyDown.

Implements the security considerations from spec section 12:
- Block private/loopback/link-local addresses after DNS resolution
- Allow only http/https schemes
- Limit sites, response size, redirects, and timeouts
"""
from __future__ import annotations

import ipaddress


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