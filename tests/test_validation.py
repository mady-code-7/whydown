"""Unit tests for validate_site_input (spec Phase 5, part B-1).

These tests cover only text-level validation — no database lookups, no
DNS resolution and no network access of any kind.
"""

from __future__ import annotations

import pytest
from fake_db import FakeConnection

from app.validation import (
    ValidationError,
    _no_credentials,
    _reject_schemes,
    check_site_limits,
    validate_site_input,
)

# ----------------------------------------------------------------------

class TestValidInput:
    def test_all_fields_valid(self):
        result = validate_site_input({
            "name": "Example site",
            "url": "https://example.com/page",
            "expected_status": 200,
            "keyword": "Welcome",
            "slow_threshold_ms": 5000,
        })
        assert result["name"] == "Example site"
        assert result["url"] == "https://example.com/page"
        assert result["expected_status"] == 200
        assert result["keyword"] == "Welcome"
        assert result["slow_threshold_ms"] == 5000

    def test_minimal_input_uses_defaults(self):
        result = validate_site_input({
            "name": "Minimal",
            "url": "http://example.com",
        })
        assert result["name"] == "Minimal"
        assert result["url"] == "http://example.com"
        assert result["expected_status"] == 200
        assert result["keyword"] is None
        assert result["slow_threshold_ms"] == 5000

    def test_https_is_accepted(self):
        result = validate_site_input({
            "name": "HTTPS site",
            "url": "https://example.com",
        })
        assert result["url"] == "https://example.com"

    def test_http_is_accepted(self):
        result = validate_site_input({
            "name": "HTTP site",
            "url": "http://example.com",
        })
        assert result["url"] == "http://example.com"


# ----------------------------------------------------------------------
# Trimming
# ----------------------------------------------------------------------

class TestTrimming:
    def test_name_is_trimmed(self):
        result = validate_site_input({
            "name": "  Example site  ",
            "url": "https://example.com",
        })
        assert result["name"] == "Example site"

    def test_name_trimmed_too_short_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "   ",
                "url": "https://example.com",
            })

    def test_whitespace_only_name_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "\t\n",
                "url": "https://example.com",
            })


# ----------------------------------------------------------------------
# Bad schemes
# ----------------------------------------------------------------------

class TestBadSchemes:
    @pytest.mark.parametrize("scheme", ["file", "ftp", "gopher", "javascript", "data"])
    def test_rejected_scheme_is_refused(self, scheme):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": f"{scheme} site",
                "url": f"{scheme}://example.com/path",
            })

    def test_empty_scheme_is_refused(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "No scheme",
                "url": "//example.com/path",
            })


# ----------------------------------------------------------------------
# URL with credentials
# ----------------------------------------------------------------------

class TestCredentials:
    def test_url_with_username_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "Creds site",
                "url": "https://user@example.com/page",
            })

    def test_url_with_password_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "Creds site",
                "url": "https://user:pass@example.com/page",
            })

    def test_url_without_credentials_is_accepted(self):
        result = validate_site_input({
            "name": "No creds",
            "url": "https://example.com/page",
        })
        assert result["url"] == "https://example.com/page"


# ----------------------------------------------------------------------
# Empty / too long names and URLs
# ----------------------------------------------------------------------

class TestLengthLimits:
    def test_empty_name_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "",
                "url": "https://example.com",
            })

    def test_name_over_100_chars_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "a" * 101,
                "url": "https://example.com",
            })

    def test_name_exactly_100_chars_is_accepted(self):
        result = validate_site_input({
            "name": "a" * 100,
            "url": "https://example.com",
        })
        assert result["name"] == "a" * 100

    def test_url_over_2048_chars_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "Long URL",
                "url": "https://example.com/" + "x" * 2048,
            })

    def test_url_exactly_2048_chars_is_accepted(self):
        path = "x" * (2048 - len("https://example.com/"))
        result = validate_site_input({
            "name": "Boundary URL",
            "url": "https://example.com/" + path,
        })
        assert len(result["url"]) == 2048


# ----------------------------------------------------------------------
# Control characters
# ----------------------------------------------------------------------

class TestControlCharacters:
    @pytest.mark.parametrize("ctrl", ["\x00", "\n", "\r", "\t", "\x1f", "\x7f"])
    def test_control_char_in_name_is_rejected(self, ctrl):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": f"site{ctrl}name",
                "url": "https://example.com",
            })


# ----------------------------------------------------------------------
# expected_status
# ----------------------------------------------------------------------

class TestExpectedStatus:
    def test_string_expected_status_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "Site",
                "url": "https://example.com",
                "expected_status": "200",
            })

    def test_float_expected_status_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "Site",
                "url": "https://example.com",
                "expected_status": 200.5,
            })

    def test_bool_expected_status_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "Site",
                "url": "https://example.com",
                "expected_status": True,
            })

    def test_below_100_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "Site",
                "url": "https://example.com",
                "expected_status": 99,
            })

    def test_above_599_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "Site",
                "url": "https://example.com",
                "expected_status": 600,
            })

    def test_100_is_accepted(self):
        result = validate_site_input({
            "name": "Site",
            "url": "https://example.com",
            "expected_status": 100,
        })
        assert result["expected_status"] == 100

    def test_599_is_accepted(self):
        result = validate_site_input({
            "name": "Site",
            "url": "https://example.com",
            "expected_status": 599,
        })
        assert result["expected_status"] == 599


# ----------------------------------------------------------------------
# Wrong types / missing fields
# ----------------------------------------------------------------------

class TestWrongTypesAndMissing:
    def test_none_data_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input(None)  # type: ignore[arg-type]

    def test_name_not_string_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": 123,
                "url": "https://example.com",
            })

    def test_url_not_string_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "Site",
                "url": None,  # type: ignore[arg-type]
            })

    def test_missing_name_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "url": "https://example.com",
            })

    def test_missing_url_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "Site",
            })


# ----------------------------------------------------------------------
# Malformed URLs that make urllib.parse.urlparse raise
# ----------------------------------------------------------------------

class TestMalformedUrls:
    def test_malformed_ipv6_url_is_rejected(self):
        # urlparse() raises ValueError on a malformed IPv6 literal (for
        # example a missing closing bracket); the scheme-check helper must
        # catch that and refuse the URL.
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "Bad IPv6",
                "url": "http://[::1",
            })


# ----------------------------------------------------------------------
# Defensive branches in URL helpers (direct calls)
# ----------------------------------------------------------------------

class TestUrlHelperExceptionBranches:
    def test_no_credentials_returns_false_on_malformed_url(self):
        # Same malformed IPv6 URL makes urlparse raise inside _no_credentials;
        # the helper must swallow the error and report "has credentials".
        assert _no_credentials("http://[::1") is False

    def test_reject_schemes_returns_true_on_malformed_url(self):
        # A malformed URL defaults to "not rejected", so _reject_schemes returns
        # True (allow) when urlparse blows up.
        assert _reject_schemes("http://[::1") is True

    def test_reject_schemes_returns_true_for_empty_scheme(self):
        # A URL without a scheme (empty scheme) is treated as not rejected.
        assert _reject_schemes("//example.com/path") is True

    def test_reject_schemes_returns_true_for_allowed_scheme(self):
        # The final return branch: a scheme that is not in the rejected list
        # is accepted (returns True).
        assert _reject_schemes("https://example.com") is True
        assert _reject_schemes("http://example.com") is True


# ----------------------------------------------------------------------
# Whitespace inside the URL
# ----------------------------------------------------------------------

class TestUrlWhitespace:
    def test_url_with_whitespace_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "WS site",
                "url": "https://example.com/ path",
            })


# ----------------------------------------------------------------------
# URL without a hostname
# ----------------------------------------------------------------------

class TestUrlWithoutHostname:
    def test_url_without_hostname_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "No host",
                "url": "https://",
            })

    def test_url_with_empty_netloc_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "No host",
                "url": "http:///path/to/resource",
            })


# ----------------------------------------------------------------------
# expected_status: types that fall into the final ``else`` branch
# ----------------------------------------------------------------------

class TestExpectedStatusOtherTypes:
    def test_list_expected_status_is_rejected(self):
        with pytest.raises(ValidationError, match="must be an integer"):
            validate_site_input({
                "name": "Site",
                "url": "https://example.com",
                "expected_status": [],
            })

    def test_dict_expected_status_is_rejected(self):
        with pytest.raises(ValidationError, match="must be an integer"):
            validate_site_input({
                "name": "Site",
                "url": "https://example.com",
                "expected_status": {"status": 200},
            })


# ----------------------------------------------------------------------
# keyword: wrong type
# ----------------------------------------------------------------------

class TestKeywordType:
    def test_keyword_integer_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "Site",
                "url": "https://example.com",
                "keyword": 123,
            })

    def test_keyword_list_is_rejected(self):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "Site",
                "url": "https://example.com",
                "keyword": ["a", "b"],
            })


# ----------------------------------------------------------------------
# slow_threshold_ms: wrong type and positivity
# ----------------------------------------------------------------------

class TestSlowThreshold:
    @pytest.mark.parametrize("bad", ["5000", 5000.5, None, True])
    def test_slow_threshold_ms_wrong_type_is_rejected(self, bad):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "Site",
                "url": "https://example.com",
                "slow_threshold_ms": bad,
            })

    @pytest.mark.parametrize("bad", [0, -1, -500])
    def test_slow_threshold_ms_below_one_is_rejected(self, bad):
        with pytest.raises(ValidationError):
            validate_site_input({
                "name": "Site",
                "url": "https://example.com",
                "slow_threshold_ms": bad,
            })


# ----------------------------------------------------------------------
# check_site_limits
# ----------------------------------------------------------------------


class TestSiteLimits:
    def test_under_limit_with_unique_url_is_accepted(self):
        conn = FakeConnection([
            {"rows": [{"count": 5}], "rowcount": 1},
            {"rows": [], "rowcount": 0},
        ])
        assert check_site_limits(conn=conn, url="https://example.com", max_sites=20) is None

    def test_at_limit_is_rejected(self):
        conn = FakeConnection([
            {"rows": [{"count": 20}], "rowcount": 1},
        ])
        with pytest.raises(ValidationError, match="Maximum"):
            check_site_limits(conn=conn, url="https://example.com", max_sites=20)

    def test_over_limit_is_rejected(self):
        conn = FakeConnection([
            {"rows": [{"count": 21}], "rowcount": 1},
        ])
        with pytest.raises(ValidationError, match="Maximum"):
            check_site_limits(conn=conn, url="https://example.com", max_sites=20)

    def test_duplicate_url_is_rejected(self):
        conn = FakeConnection([
            {"rows": [{"count": 1}], "rowcount": 1},
            {"rows": [{"id": 2}], "rowcount": 1},
        ])
        with pytest.raises(ValidationError, match="already exists"):
            check_site_limits(conn=conn, url="https://example.com", max_sites=20)

    def test_duplicate_url_with_exclude_id_is_accepted(self):
        conn = FakeConnection([
            {"rows": [{"count": 1}], "rowcount": 1},
            {"rows": [], "rowcount": 0},
        ])
        assert check_site_limits(
            conn=conn, url="https://example.com", exclude_id=1, max_sites=20
        ) is None

    def test_new_site_at_limit_but_unique_url(self):
        conn = FakeConnection([
            {"rows": [{"count": 19}], "rowcount": 1},
            {"rows": [], "rowcount": 0},
        ])
        assert check_site_limits(conn=conn, url="https://example.com", max_sites=20) is None

    def test_custom_max_sites(self):
        conn = FakeConnection([
            {"rows": [{"count": 3}], "rowcount": 1},
        ])
        with pytest.raises(ValidationError, match="Maximum"):
            check_site_limits(conn=conn, url="https://example.com", max_sites=3)

    def test_custom_max_sites_under_limit(self):
        conn = FakeConnection([
            {"rows": [{"count": 2}], "rowcount": 1},
            {"rows": [], "rowcount": 0},
        ])
        assert check_site_limits(conn=conn, url="https://example.com", max_sites=3) is None
