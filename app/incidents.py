"""Incident state machine for WhyDown (Phase 4 part A).

The state machine reacts to the result of one check that was just stored:

* an **up** result (OK, SLOW or DNS_SLOW) while no incident is open does
  nothing;
* an **up** result while an incident is open **closes** that incident - this
  is the recovery;
* a **failure** adds to the counter of consecutive failures; as soon as the
  counter reaches the threshold and no incident is open, **one** incident is
  opened. Every further failure is ignored while the incident stays open, so
  a long outage never produces a second incident.

Rules of this module:

* The state lives in the database, never in memory. A restart of the free
  Render instance therefore never loses an incident and never sends a
  duplicate alert (spec section 8.4).
* The consecutive failures are counted from the stored checks, so the counter
  survives a restart as well.
* Every failure is caught: one site with a broken incident state must never
  stop the check run (spec section 10).
* The function returns a small value - ``"opened"``, ``"closed"`` or ``None``
  - so the caller (Phase 4 part B) can send the matching alert.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import config
from .db import (
    UP_RESULTS,
    close_incident,
    get_open_incident,
    open_incident,
    recent_results,
)

logger = logging.getLogger(__name__)

# How many recent checks are read to count the consecutive failures. The
# threshold is small, so a handful of rows is enough.
RECENT_LIMIT = 20


def _site_id(site: Any) -> int | None:
    """Return the id of a site row, or None when it has none.

    The site may be a mapping (as the database layer returns it) or an object
    with an ``id`` attribute.
    """
    if site is None:
        return None
    if isinstance(site, dict):
        value = site.get("id")
    else:
        value = getattr(site, "id", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _result_code(result: Any) -> str:
    """Return the classification code of a check result.

    The result may be a ``checker.CheckResult``, a mapping or a plain string.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        value = result.get("result")
    else:
        value = getattr(result, "result", None)
    return str(value) if value is not None else ""


def _threshold() -> int:
    """Return how many consecutive failures open an incident."""
    return int(config.thresholds.incident_threshold)


def _count_consecutive_failures(codes: list[str]) -> int:
    """Count the failures at the start of ``codes`` (newest first).

    The count stops at the first up result: a single successful check in
    between breaks the chain of failures (spec section 8.4).
    """
    count = 0
    for code in codes:
        if code in UP_RESULTS:
            break
        count += 1
    return count


def process_check(conn: Any, site: Any, result: Any) -> str | None:
    """Feed one stored check into the incident state machine.

    ``conn`` is the connection of the run (the same one the check was stored
    with), ``site`` is the site the check belongs to and ``result`` is the
    classification of the check.

    Returns ``"opened"`` when an incident was opened, ``"closed"`` when an
    incident was closed and ``None`` when nothing changed. The function never
    raises: a database error is logged and the run continues.
    """
    try:
        return _process_check(conn, site, result)
    except Exception as exc:  # noqa: BLE001 - one site must not stop the run
        # Only the type of the error is logged: the message of a database
        # error may contain the connection string (spec section 12).
        logger.error("The incident state of a site could not be read (%s)", type(exc).__name__)
        return None


def _process_check(conn: Any, site: Any, result: Any) -> str | None:
    """The state machine itself; every error is caught by ``process_check``."""
    site_id = _site_id(site)
    if site_id is None:
        return None

    code = _result_code(result)

    # An up result: close an open incident, this is the recovery.
    if code in UP_RESULTS:
        incident = get_open_incident(site_id, conn=conn)
        if not incident:
            return None
        close_incident(int(incident["id"]), conn=conn)
        logger.info("Incident %s of site %s closed (recovery)", incident["id"], site_id)
        return "closed"

    # A failure: count the consecutive failures and open an incident when the
    # threshold is reached. The count is read from the database, so it also
    # survives a restart of the app.
    threshold = _threshold()
    codes = recent_results(site_id, limit=max(RECENT_LIMIT, threshold), conn=conn)
    consecutive = _count_consecutive_failures(codes)
    if consecutive < threshold:
        return None

    # Never open a second incident while one is open.
    incident = get_open_incident(site_id, conn=conn)
    if incident:
        return None

    incident_id = open_incident(
        site_id,
        failure_type=code,
        consecutive_failures=consecutive,
        conn=conn,
    )
    logger.info(
        "Incident %s of site %s opened (%s, %s consecutive failures)",
        incident_id,
        site_id,
        code,
        consecutive,
    )
    return "opened"
