"""Normalise and validate a URL for public checking.

Used by the public check flow to canonicalise a user-supplied URL.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import urllib.parse
from contextlib import suppress
from typing import Any

from flask import Flask, jsonify, request

from app import db
from app.checker import check_url
from app.config import PublicConfig
from app.security import SecurityError, check_url_target, hash_visitor
from app.validation import ValidationError, validate_site_input

logger = logging.getLogger(__name__)

# HTTP code for each status that run_public_check can return.
_STATUS_CODES = {"ok": 200, "cached": 200, "invalid_url": 400, "blocked": 400, "rate_limited": 429}


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


def _public_config() -> PublicConfig:
    """Return the public configuration from the app config."""
    from app.config import config

    return config.public


def _open_connection() -> Any:
    """Return a new database connection."""
    return db._connect()


def _cors(response: Any) -> Any:
    """Add CORS headers to the response and return it."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Max-Age"] = "86400"
    return response


def _json(body: dict[str, Any], status: int) -> Any:
    """Return a JSON response with CORS headers and the given status code."""
    response = jsonify(body)
    response.status_code = status
    return _cors(response)


def register_public_routes(app: Flask) -> None:
    """Register the public check endpoint."""

    @app.route("/api/public/check", methods=["POST", "OPTIONS"])
    def api_public_check():
        if request.method == "OPTIONS":
            return _cors(app.response_class(status=204))

        data = request.get_json(silent=True)
        url = data.get("url") if isinstance(data, dict) else None
        if not isinstance(url, str):
            return _json({"error": "Send a JSON body with a url."}, 400)

        conn = None
        try:
            conn = _open_connection()
            ip = client_ip(request.headers.get("X-Forwarded-For"), request.remote_addr)
            outcome = run_public_check(url, ip, _public_config(), conn=conn)
        except Exception as exc:  # noqa: BLE001 - any failure is a 503
            logger.error("Public check failed (%s)", type(exc).__name__)
            return _json({"error": "The check is not available."}, 503)
        finally:
            if conn is not None:
                with suppress(Exception):
                    conn.close()

        value = outcome.get("checked_at")
        if hasattr(value, "isoformat"):
            outcome["checked_at"] = value.isoformat()

        return _json(outcome, _STATUS_CODES.get(outcome["status"], 500))