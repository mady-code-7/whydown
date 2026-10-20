"""Tests for the SSRF filter (spec section 12).

The application fetches URLs the user supplies, so every address it resolves
has to be checked before a connection is opened. Anything that is not
globally routable is blocked - and an address that cannot be parsed is
blocked as well, because the safe answer to "is this address safe?" is
"no" when the address is not an address.
"""

from __future__ import annotations

import pytest

from app.security import is_private_ip


class TestBlockedAddresses:
    """Private, loopback and link-local addresses are blocked."""

    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",  # loopback
            "127.1.2.3",  # the whole 127.0.0.0/8 range
            "10.0.0.1",  # private
            "172.16.0.1",  # private
            "192.168.1.1",  # private
            "169.254.169.254",  # link-local (cloud metadata)
            "0.0.0.0",  # unspecified
            "240.0.0.1",  # reserved
            "255.255.255.255",  # broadcast
            "100.64.0.1",  # shared address space (carrier grade NAT)
        ],
    )
    def test_non_global_addresses_are_blocked(self, ip):
        assert is_private_ip(ip) is True

    def test_multicast_addresses_are_not_blocked(self):
        """The filter follows ``ipaddress.is_global``.

        The IPv4 multicast range (224.0.0.0/4) counts as global by that
        definition, so it passes the filter. The docstring of
        ``is_private_ip()`` lists multicast among the blocked ranges - the
        two disagree, and this test pins down what the code actually does.
        """
        assert is_private_ip("224.0.0.1") is False


class TestAllowedAddresses:
    """Public addresses are allowed."""

    @pytest.mark.parametrize(
        "ip",
        [
            "8.8.8.8",
            "1.1.1.1",
            "93.184.216.34",
            "2606:2800:220:1:248:1893:25c8:1946",
        ],
    )
    def test_global_addresses_are_allowed(self, ip):
        assert is_private_ip(ip) is False


class TestUnparsableAddresses:
    """An address that is not an address is blocked."""

    @pytest.mark.parametrize(
        "value",
        ["", "not-an-ip", "999.999.999.999", "127.0.0", "localhost"],
    )
    def test_invalid_addresses_are_blocked(self, value):
        assert is_private_ip(value) is True
