"""Pure validation for site input (spec Phase 5, part B-1).

This function validates and cleans the data a user submits when adding
or editing a monitored site. It performs only text-level checks — no
DNS lookups, no network I/O, and no database access.
"""

from __future__ import annotations

import urllib.parse


class ValidationError(ValueError):
    """Raised when site input fails validation."""


def _has_control_chars(s: str) -> bool:
    """Return True if *s* contains any Unicode control character."""
    return any(ord(ch) <= 0x1F or ord(ch) == 0x7F for ch in s)


def _scheme_is_allowed(url: str, allowed: tuple[str, ...] = ("http", "https")) -> bool:
    """Check that the URL scheme is one of the allowed ones.

    Returns False if the scheme is empty, disallowed, or if the URL
    cannot be parsed at all.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except (ValueError, TypeError, AttributeError):
        return False
    if not parsed.scheme:
        return False
    return parsed.scheme in allowed


def _no_credentials(url: str) -> bool:
    """Return True when the URL has no username or password components."""
    try:
        parsed = urllib.parse.urlparse(url)
    except (ValueError, TypeError, AttributeError):
        return False
    return parsed.username is None and parsed.password is None


def _no_whitespace(url: str) -> bool:
    """Return True when the URL string contains no whitespace characters."""
    return not any(ch.isspace() for ch in url)


def _reject_schemes(url: str, rejected: tuple[str, ...] = (
    "file", "ftp", "gopher", "javascript", "data",
)) -> bool:
    """Return True when the URL scheme is not one of the rejected ones."""
    try:
        parsed = urllib.parse.urlparse(url)
    except (ValueError, TypeError, AttributeError):
        return True
    if not parsed.scheme:
        return True
    return parsed.scheme not in rejected


def validate_site_input(data: dict) -> dict:
    """Validate and clean a site input dict.

    Parameters
    ----------
    data : dict
        Mapping of field names to user-supplied values.  Expected keys
        are ``name``, ``url``, ``expected_status``, ``keyword`` and
        ``slow_threshold_ms``.  Only the keys that are present are
        validated; missing optional fields fall back to defaults.

    Returns
    -------
    dict
        A new dict with *all* the original keys mapped to their cleaned
        / default-ified values.

    Raises
    ------
    ValidationError
        If any field fails its validation rule.  The exception message
        describes the problem clearly.
    """
    if not isinstance(data, dict):
        raise ValidationError("Site data must be a dictionary.")

    result: dict[str, object] = {}

    # ------------------------------------------------------------------
    # name: 1..100 chars after trimming, no control characters
    # ------------------------------------------------------------------
    raw_name = data.get("name", "")
    if not isinstance(raw_name, str):
        raise ValidationError("Name must be a text value.")

    name = raw_name.strip()
    if len(name) < 1 or len(name) > 100:
        raise ValidationError(
            f"Name must be 1 to 100 characters after trimming; "
            f"got {len(name)} characters."
        )
    if _has_control_chars(name):
        raise ValidationError("Name must not contain control characters.")
    result["name"] = name

    # ------------------------------------------------------------------
    # url: http/https only, max 2048 chars, must have a hostname,
    #      no username/password, no whitespace, reject forbidden schemes
    # ------------------------------------------------------------------
    raw_url = data.get("url", "")
    if not isinstance(raw_url, str):
        raise ValidationError("URL must be a text value.")

    url = raw_url
    if len(url) > 2048:
        raise ValidationError("URL must not exceed 2048 characters.")

    if not _scheme_is_allowed(url, allowed=("http", "https")):
        raise ValidationError(
            "URL scheme must be http or https; other schemes are not allowed."
        )

    if not _no_credentials(url):
        raise ValidationError(
            "URL must not contain a username or password."
        )

    if not _no_whitespace(url):
        raise ValidationError("URL must not contain whitespace characters.")

    if not urllib.parse.urlparse(url).hostname:
        raise ValidationError("URL must contain a hostname.")

    result["url"] = url

    # ------------------------------------------------------------------
    # expected_status: integer 100..599, default 200
    # ------------------------------------------------------------------
    raw_expected = data.get("expected_status", 200)

    if isinstance(raw_expected, bool):
        raise ValidationError("expected_status must be an integer, not a boolean.")

    if isinstance(raw_expected, (int, float)):
        # Reject floats that aren't exact ints
        if isinstance(raw_expected, float) and raw_expected != int(raw_expected):
            raise ValidationError(
                "expected_status must be an integer whole number; "
                f"got a non-integer float {raw_expected!r}."
            )
        status = int(raw_expected)
    elif isinstance(raw_expected, str):
        raise ValidationError(
            f"expected_status must be an integer; got a string {raw_expected!r}."
        )
    else:
        raise ValidationError(
            f"expected_status must be an integer; got {type(raw_expected).__name__}."
        )

    if not (100 <= status <= 599):
        raise ValidationError(
            f"expected_status must be between 100 and 599; got {status}."
        )

    result["expected_status"] = status

    # ------------------------------------------------------------------
    # keyword: optional text field (may be None or omitted)
    # ------------------------------------------------------------------
    raw_kw = data.get("keyword")
    if raw_kw is not None and not isinstance(raw_kw, str):
        raise ValidationError("keyword must be a text value or None.")
    result["keyword"] = raw_kw if raw_kw is not None else None

    # ------------------------------------------------------------------
    # slow_threshold_ms: integer (default 5000 from schema)
    # ------------------------------------------------------------------
    raw_thresh = data.get("slow_threshold_ms", 5000)
    if not isinstance(raw_thresh, int) or isinstance(raw_thresh, bool):
        raise ValidationError(
            f"slow_threshold_ms must be an integer; got {type(raw_thresh).__name__}."
        )
    if raw_thresh < 1:
        raise ValidationError(
            "slow_threshold_ms must be a positive integer."
        )
    result["slow_threshold_ms"] = raw_thresh

    return result