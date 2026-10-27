"""Normalise and validate a URL for public checking.

Used by the public check flow to canonicalise a user-supplied URL.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.parse
from typing import Any

from app import db
from app.checker import check_url
from app.config import PublicConfig
from app.security import SecurityError, check_url_target, hash_visitor
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


def _summarise(result: Any) -> dict[str, Any]:
    """Return a summary dict with exactly 12 keys from the check result.

    error_message is cut to its first 300 characters if present.
    """
    summary: dict[str, Any] = {
        "result": result.result,
        "failed_stage": result.failed_stage,
        "status_code": result.status_code,
        "dns_ms": result.dns_ms,
        "tcp_ms": result.tcp_ms,
        "tls_ms": result.tls_ms,
        "ttfb_ms": result.ttfb_ms,
        "total_ms": result.total_ms,
        "error_message": result.error_message,
        "cert_expired": result.cert_expired,
        "cert_expiring_soon": result.cert_expiring_soon,
        "cert_invalid": result.cert_invalid,
    }
    message = summary["error_message"]
    if message is not None:
        summary["error_message"] = message[:300]
    return summary


def run_public_check(url: str, visitor_ip: str, config: PublicConfig, conn: Any | None = None) -> dict[str, Any]:
    """Run one public check for a URL from a visitor, honoring cache and rate limits."""
    try:
        normalised = normalise_url(url)
    except ValidationError:
        return {"status": "invalid_url", "message": "Not a valid website address."}

    visitor_hash = hash_visitor(visitor_ip, config.ip_hash_key)

    recent = db.get_recent_public_check(visitor_hash, normalised, 1, conn=conn)
    if recent is not None:
        return {
            "status": "cached",
            "normalised_url": normalised,
            "result": json.loads(recent["result_summary"]),
            "checked_at": recent["checked_at"],
        }

    count = db.count_public_checks(visitor_hash, 1, conn=conn)
    if count >= config.rate_limit_per_hour:
        return {"status": "rate_limited", "message": "Too many checks this hour. Try again later."}

    try:
        check_url_target(normalised)
    except SecurityError:
        return {"status": "blocked", "message": "This address cannot be checked."}

    result = check_url(normalised)
    summary = _summarise(result)
    db.save_public_check(visitor_hash, normalised, json.dumps(summary), conn=conn)
    return {"status": "ok", "normalised_url": normalised, "result": summary}


def client_ip(forwarded_for: str | None, remote_addr: str | None) -> str:
    """Return the visitor's IP, trusting the first X-Forwarded-For entry.

    Behind Render the first X-Forwarded-For entry is the visitor; a visitor can
    fake it, which is accepted for this temporary page.
    """
    if forwarded_for is not None and forwarded_for != "":
        first = forwarded_for.split(",")[0].strip()
        try:
            parsed = ipaddress.ip_address(first)
        except ValueError:
            parsed = None
        if parsed is not None:
            return str(parsed)
    if remote_addr is not None and remote_addr.strip() != "":
        return remote_addr.strip()
    return "unknown"