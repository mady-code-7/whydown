"""Normalise and validate a URL for public checking.

Used by the public check flow to canonicalise a user-supplied URL.
"""

from __future__ import annotations

import urllib.parse

from app.validation import ValidationError, validate_site_input


def normalise_url(url: str) -> str:
    """Return a normalised URL with lowercased scheme and host, default port
    stripped, fragment dropped, and path/query preserved as-is.

    The URL is first stripped of surrounding whitespace and validated by
    ``validate_site_input`` (which enforces http/https, no credentials, a
    hostname, and no whitespace). Any ``ValidationError`` is propagated.

    After validation the URL is parsed and rebuilt so that:
    - scheme is lowercase
    - host is lowercase (already done by urlsplit)
    - IPv6 literals are wrapped in brackets
    - default ports (80 for http, 443 for https) are omitted
    - non-default ports are kept
    - path is "/" if empty
    - query is preserved with leading "?"
    - fragment is discarded
    """
    if isinstance(url, str):
        cleaned = url.strip()
        validated = validate_site_input({"name": "public", "url": cleaned})["url"]
    else:
        validated = validate_site_input({"name": "public", "url": url})["url"]

    parts = urllib.parse.urlsplit(validated)
    try:
        port = parts.port
    except ValueError:
        raise ValidationError("Not a valid website address.")

    scheme = parts.scheme.lower()
    host = parts.hostname
    if host is None:
        raise ValidationError("Not a valid website address.")
    if ":" in host:
        host = f"[{host}]"

    default_port = 80 if scheme == "http" else 443
    if port is not None and port != default_port:
        netloc = f"{host}:{port}"
    else:
        netloc = host

    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    return f"{scheme}://{netloc}{path}"