"""Tests for the SSRF filter (spec section 12).

The application fetches URLs the user supplies, so every address it resolves
has to be checked before a connection is opened. Anything that is not
globally routable is blocked - and an address that cannot be parsed is
blocked as well, because the safe answer to "is this address safe?" is
"no" when the address is not an address.
"""

from __future__ import annotations

import socket

import pytest

from app.security import (
    SecurityError,
    _is_local_hostname,
    _try_parse_ip_literal,
    check_url_target,
    is_private_ip,
)


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


class TestSpecialRanges:
    """Multicast, reserved and unspecified addresses are blocked as well.

    ``ipaddress.is_global`` counts the IPv4 multicast range as global on
    some Python versions, so these ranges are blocked by name and not only
    through the is_global check.
    """

    @pytest.mark.parametrize(
        "ip",
        [
            "224.0.0.1",  # multicast
            "239.255.255.250",  # multicast (SSDP)
            "240.0.0.1",  # reserved
            "255.255.255.255",  # broadcast (inside the reserved range)
            "0.0.0.0",  # unspecified
        ],
    )
    def test_special_ranges_are_blocked(self, ip):
        assert is_private_ip(ip) is True


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


class TestLocalHostnames:
    """Local-only hostname suffixes are treated as local."""

    @pytest.mark.parametrize(
        "hostname",
        [
            "localhost",
            "LOCALHOST",
            "Localhost",
            "localhost.localdomain",
            "myapp.localhost",
            "myapp.local",
            "myapp.internal",
            "internal.service.internal",
        ],
    )
    def test_local_hostnames_are_detected(self, hostname):
        assert _is_local_hostname(hostname) is True

    @pytest.mark.parametrize(
        "hostname",
        [
            "example.com",
            "sub.example.com",
            "example.internal.evil.com",  # ends with .com, not .internal
            "evil-local.com",
        ],
    )
    def test_public_hostnames_are_allowed(self, hostname):
        assert _is_local_hostname(hostname) is False


class TestIpLiteralParsing:
    """The helper parses plain and unusual IP literals."""

    def test_ipv4_literal(self):
        assert _try_parse_ip_literal("127.0.0.1") is not None

    def test_ipv4_literal_value(self):
        addr = _try_parse_ip_literal("127.0.0.1")
        assert addr is not None
        assert str(addr) == "127.0.0.1"

    def test_ipv6_literal(self):
        addr = _try_parse_ip_literal("2001:db8::1")
        assert addr is not None
        assert str(addr) == "2001:db8::1"

    def test_ipv4_mapped_ipv6_is_parsed(self):
        addr = _try_parse_ip_literal("::ffff:127.0.0.1")
        assert addr is not None
        assert str(addr) == "::ffff:127.0.0.1"

    def test_decimal_ip_literal(self):
        addr = _try_parse_ip_literal("2130706433")  # 127.0.0.1 in decimal
        assert addr is not None
        assert str(addr) == "127.0.0.1"

    def test_hex_ip_literal(self):
        addr = _try_parse_ip_literal("0x7f000001")  # 127.0.0.1 in hex
        assert addr is not None
        assert str(addr) == "127.0.0.1"

    def test_hostname_is_not_an_ip_literal(self):
        assert _try_parse_ip_literal("example.com") is None

    def test_empty_string_is_not_an_ip_literal(self):
        assert _try_parse_ip_literal("") is None


class TestCheckUrlTarget:
    """The end-to-end SSRF filter blocks unsafe URLs."""

    def _resolver_for(self, *addresses: str) -> list:
        """Build a fake resolver returning the given addresses."""

        def resolver(hostname, port, family, type_):
            # Return sockaddr tuples of the form the code expects
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr,))
                for addr in addresses
            ]

        return resolver

    def test_public_url_resolves(self):
        resolved = check_url_target(
            "https://example.com/",
            resolver=self._resolver_for("1.2.3.4", "5.6.7.8"),
        )
        assert resolved == ["1.2.3.4", "5.6.7.8"]

    def test_localhost_hostname_is_blocked(self):
        with pytest.raises(SecurityError):
            check_url_target("http://localhost/", resolver=self._resolver_for("1.2.3.4"))

    def test_localhost_subdomain_is_blocked(self):
        with pytest.raises(SecurityError):
            check_url_target("http://app.localhost/", resolver=self._resolver_for("1.2.3.4"))

    def test_local_tld_is_blocked(self):
        with pytest.raises(SecurityError):
            check_url_target("http://app.local/", resolver=self._resolver_for("1.2.3.4"))

    def test_internal_tld_is_blocked(self):
        with pytest.raises(SecurityError):
            check_url_target("http://app.internal/", resolver=self._resolver_for("1.2.3.4"))

    def test_ipv4_loopback_is_blocked(self):
        with pytest.raises(SecurityError):
            check_url_target("http://127.0.0.1/", resolver=self._resolver_for("1.2.3.4"))

    def test_ipv4_private_is_blocked(self):
        with pytest.raises(SecurityError):
            check_url_target("http://10.0.0.1/", resolver=self._resolver_for("1.2.3.4"))

    def test_ipv4_link_local_is_blocked(self):
        with pytest.raises(SecurityError):
            check_url_target("http://169.254.169.254/", resolver=self._resolver_for("1.2.3.4"))

    def test_ipv6_loopback_is_blocked(self):
        with pytest.raises(SecurityError):
            check_url_target("http://[::1]/", resolver=self._resolver_for("::1"))

    def test_ipv6_private_is_blocked(self):
        with pytest.raises(SecurityError):
            check_url_target("http://[fe80::1]/", resolver=self._resolver_for("fe80::1"))

    def test_ipv4_mapped_ipv6_loopback_is_blocked(self):
        with pytest.raises(SecurityError):
            check_url_target(
                "http://[::ffff:127.0.0.1]/",
                resolver=self._resolver_for("::ffff:127.0.0.1"),
            )

    def test_decimal_ipv4_is_blocked(self):
        with pytest.raises(SecurityError):
            check_url_target("http://2130706433/", resolver=self._resolver_for("127.0.0.1"))

    def test_hex_ipv4_is_blocked(self):
        with pytest.raises(SecurityError):
            check_url_target("http://0x7f000001/", resolver=self._resolver_for("127.0.0.1"))

    def test_mixed_public_and_private_is_blocked(self):
        with pytest.raises(SecurityError):
            check_url_target(
                "https://dual.example.com/",
                resolver=self._resolver_for("1.2.3.4", "10.0.0.1"),
            )

    def test_unresolvable_host_is_blocked(self):
        def failing_resolver(hostname, port, family, type_):
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

        with pytest.raises(SecurityError):
            check_url_target("https://nonexistent.example.com/", resolver=failing_resolver)

    def test_no_addresses_returned_is_blocked(self):
        def empty_resolver(hostname, port, family, type_):
            return []

        with pytest.raises(SecurityError):
            check_url_target("https://empty.example.com/", resolver=empty_resolver)
