"""Alert sending for WhyDown (Phase 4 part B).

The project alerts through Telegram: a short plain text message is sent to
one chat when a site goes down and when it comes back (spec section 8.4).

Rules of this module:

* The bot token and the chat id come from the environment
  (``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID``). They are never
  hardcoded, never logged and never part of an error message. The token is
  part of the API URL, so every error text is scrubbed before it is used
  (spec section 12).
* When one of the two is missing the alert is skipped quietly and the run
  continues: an installation without Telegram still has to monitor its
  sites.
* Only the standard library is used (``urllib``), so the project keeps its
  small dependency list on the free tier.
* Every attempt - successful or not - is written to the ``alert_log`` table.
  That log is the only memory of what was already sent, so a second DOWN
  alert for the same incident is never sent, not even after a restart of
  the app (spec section 8.4, test cases T12 and T14).
* No function raises. A broken alert channel must never break a check run
  (spec section 10).

Only metadata of a site and of an incident is put into a message; a
response body never reaches an alert (spec section 12).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv

from .db import alert_exists, log_alert

logger = logging.getLogger(__name__)

# The Telegram Bot API. The bot token is appended to the path, which is why
# every error text of this module is scrubbed before it is logged.
TELEGRAM_API_URL = "https://api.telegram.org"

# Timeout of the request to Telegram in seconds. The alert is sent from the
# main thread of a check run, so it may not block the run for long.
REQUEST_TIMEOUT = 10

# The answer of Telegram is small (a JSON object). It is read with a limit,
# so a wrong endpoint cannot flood the memory of the free instance.
MAX_RESPONSE_BYTES = 4096

# The kinds of alert (spec section 8.4). They are the values of the ``kind``
# column of the alert_log table.
KIND_DOWN = "DOWN"
KIND_RECOVERY = "RECOVERY"

# The channel of the alert log.
CHANNEL_TELEGRAM = "telegram"


class TelegramError(RuntimeError):
    """Raised when a message could not be sent to Telegram.

    The message never contains the bot token: the token is part of the API
    URL, and an error of the HTTP layer may repeat that URL (spec
    section 12).
    """


# --------------------------------------------------------------------------
# The credentials
# --------------------------------------------------------------------------

def telegram_credentials() -> tuple[str, str] | None:
    """Return ``(token, chat_id)`` from the environment.

    The values are read from the environment at the moment they are needed,
    so a changed environment takes effect without a restart. ``None`` is
    returned when one of them is missing: the alerts are then skipped
    quietly and the check run continues.

    The token is never logged, never printed and never part of an error
    message (spec section 12).
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    return token, chat_id


def scrub(text: str, token: str | None = None) -> str:
    """Remove the bot token from a text so that it can be logged safely.

    The token is part of the Telegram URL, so it can end up in the message
    of an exception of the HTTP layer. Every text that is written to a log
    or printed goes through this function first.
    """
    if not text:
        return text
    if token is None:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if token:
        text = text.replace(token, "<redacted>")
    return text


# --------------------------------------------------------------------------
# The message
# --------------------------------------------------------------------------

def _field(row: Any, key: str) -> Any:
    """Read a field of a mapping or of an object with attributes."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _site_id(site: Any) -> int | None:
    """Return the id of a site row, or None when it has none."""
    value = _field(site, "id")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _incident_id(incident: Any) -> int | None:
    """Return the id of an incident row, or None when it has none."""
    value = _field(incident, "id")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    """Return a timestamp as a datetime, or None when it is not one."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def format_timestamp(value: Any) -> str:
    """Format a timestamp of an incident for the alert message."""
    moment = _as_datetime(value)
    if moment is None:
        return "unknown"
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
        return moment.strftime("%Y-%m-%d %H:%M:%S UTC")
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def format_duration(incident: Any) -> str:
    """Format the downtime of an incident as a readable duration.

    The duration is the time between the start and the end of the incident.
    An incident that is still open has no end, so its duration is unknown.
    """
    start = _as_datetime(_field(incident, "started_at"))
    end = _as_datetime(_field(incident, "ended_at"))
    if start is None or end is None:
        return "unknown"

    seconds = max(int((end - start).total_seconds()), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def build_message(kind: str, site: Any, incident: Any) -> str:
    """Build the plain text of an alert.

    A DOWN alert names the site, its URL, the failure type and the time the
    incident started. A RECOVERY alert names the site, its URL and how long
    the site was down. Only metadata is used - never a response body
    (spec section 12).
    """
    name = _field(site, "name") or "unknown site"
    url = _field(site, "url") or "unknown url"

    if kind == KIND_RECOVERY:
        return (
            "WhyDown: the site is back\n"
            f"Site: {name}\n"
            f"URL: {url}\n"
            f"Downtime: {format_duration(incident)}"
        )

    failure = _field(incident, "failure_type") or "unknown failure"
    started = format_timestamp(_field(incident, "started_at"))
    return (
        "WhyDown: the site is down\n"
        f"Site: {name}\n"
        f"URL: {url}\n"
        f"Failure: {failure}\n"
        f"Started: {started}"
    )


# --------------------------------------------------------------------------
# The Telegram API
# --------------------------------------------------------------------------

def _parse_answer(body: bytes) -> tuple[bool, str]:
    """Read the ``ok`` flag and the description of the answer of Telegram."""
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return False, "the answer was not valid JSON"
    if not isinstance(data, dict):
        return False, "the answer was not an object"
    return bool(data.get("ok")), str(data.get("description") or "")


def send_telegram(
    text: str,
    token: str,
    chat_id: str,
    timeout: int = REQUEST_TIMEOUT,
) -> None:
    """Send one plain text message to a Telegram chat.

    Raises ``TelegramError`` when the message could not be sent. The message
    of the error never contains the bot token, not even when the HTTP layer
    repeats the API URL in its own message.
    """
    url = f"{TELEGRAM_API_URL}/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200) or 200
            body = response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        raise TelegramError(
            scrub(f"Telegram answered with HTTP {exc.code}", token)
        ) from None
    except urllib.error.URLError as exc:
        raise TelegramError(
            scrub(f"Telegram could not be reached: {exc.reason}", token)
        ) from None
    except OSError as exc:
        raise TelegramError(
            scrub(f"Telegram could not be reached: {exc}", token)
        ) from None

    if status >= 300:
        raise TelegramError(scrub(f"Telegram answered with HTTP {status}", token))

    ok, description = _parse_answer(body)
    if not ok:
        raise TelegramError(
            scrub(f"Telegram refused the message: {description}", token)
        )


# --------------------------------------------------------------------------
# Sending an alert
# --------------------------------------------------------------------------

def _record_attempt(
    site_id: int,
    incident_id: int,
    kind: str,
    success: bool,
    conn: Any,
) -> None:
    """Write one attempt to the alert log.

    A failure of the log is logged and not raised: the alert itself was
    already sent (or already failed), and the run must continue.
    """
    try:
        log_alert(
            site_id=site_id,
            incident_id=incident_id,
            kind=kind,
            channel=CHANNEL_TELEGRAM,
            success=success,
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 - a failed log must not stop the run
        logger.error("The alert attempt could not be logged (%s)", type(exc).__name__)


def _send_alert(conn: Any, site: Any, incident: Any, kind: str) -> bool:
    """Send one alert and record the attempt; every error is caught here."""
    if kind not in (KIND_DOWN, KIND_RECOVERY):
        logger.error("An alert of the unknown kind %r was skipped", kind)
        return False

    credentials = telegram_credentials()
    if credentials is None:
        logger.info("Telegram is not configured; the %s alert is skipped", kind)
        return False
    token, chat_id = credentials

    site_id = _site_id(site)
    incident_id = _incident_id(incident)
    if site_id is None or incident_id is None:
        logger.error(
            "The %s alert was skipped: the site or the incident is unknown", kind
        )
        return False

    # The alert log is the only memory of what was already sent, so a second
    # DOWN alert for the same incident is never sent - not even after a
    # restart of the app (spec section 8.4, test cases T12 and T14).
    if alert_exists(incident_id, kind, conn=conn):
        logger.info("The %s alert of incident %s was already sent", kind, incident_id)
        return False

    text = build_message(kind, site, incident)
    success = False
    try:
        send_telegram(text, token, chat_id)
        success = True
        logger.info("The %s alert of incident %s was sent", kind, incident_id)
    except Exception as exc:  # noqa: BLE001 - a failed alert must not stop the run
        # The text of the error is scrubbed: the token is part of the
        # Telegram URL and must never reach a log (spec section 12).
        logger.error(
            "The %s alert of incident %s failed: %s",
            kind,
            incident_id,
            scrub(str(exc), token),
        )
    finally:
        _record_attempt(site_id, incident_id, kind, success, conn)
    return success


def send_alert(conn: Any, site: Any, incident: Any, kind: str) -> bool:
    """Send one incident alert and record the attempt.

    ``kind`` is ``"DOWN"`` or ``"RECOVERY"``. The function returns True when
    a message was sent to Telegram and False when the alert was skipped or
    when it failed. It never raises: a broken alert channel must never
    break a check run (spec section 10).
    """
    try:
        return _send_alert(conn, site, incident, kind)
    except Exception as exc:  # noqa: BLE001 - an alert must not stop the run
        logger.error(
            "The %s alert of site %s could not be sent (%s)",
            kind,
            _site_id(site),
            type(exc).__name__,
        )
        return False


# --------------------------------------------------------------------------
# Command line entry point
# --------------------------------------------------------------------------

USAGE = """usage: python -m app.alerts COMMAND

commands:
  test      Send a test message to the configured Telegram chat.
"""


def _run_test() -> int:
    """Send a test message and report whether it arrived."""
    credentials = telegram_credentials()
    if credentials is None:
        print(
            "Telegram is not configured: "
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are both needed.",
            file=sys.stderr,
        )
        return 1
    token, chat_id = credentials

    try:
        send_telegram("WhyDown test alert", token, chat_id)
    except Exception as exc:  # noqa: BLE001 - the CLI reports the error
        # The text is scrubbed: the token must never be printed (spec
        # section 12).
        print(f"Test alert failed: {scrub(str(exc), token)}", file=sys.stderr)
        return 1

    print("OK: the test message was sent.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point of ``python -m app.alerts``.

    Returns 0 on success and a non-zero code on failure. Neither a success
    nor a failure ever prints the bot token.
    """
    # The .env file is loaded on the command line entry point only, so that
    # importing this module never reads a real .env file.
    load_dotenv()

    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        print(USAGE)
        return 1

    command = argv[0]
    if command in ("-h", "--help"):
        print(USAGE)
        return 0

    if command == "test":
        return _run_test()

    print(f"unknown command {command!r}", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
