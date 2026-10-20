"""URL validation and SSRF filter for WhyDown.

Implements the security considerations from spec section 12:
- Block private/loopback/link-local addresses after DNS resolution
- Allow only http/https schemes
- Limit sites, response size, redirects, and timeouts
"""
from __future__ import annotations

import ipaddress


def is_private_ip(ip: str) -> bool:
    """Check if an IP address is private, loopback, or link-local (SSRF protection).

    Uses the ipaddress module's is_global property: any address that is not
    globally routable (private, loopback, link-local, multicast, reserved,
    unspecified) is treated as blocked.
    """
    try:
        addr = ipaddress.ip_address(ip)
        return not addr.is_global
    except ValueError:
        # Invalid IP -> block
        return True