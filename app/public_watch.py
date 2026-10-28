"""Public watches: a visitor asks for one Telegram message when a site is up.

There is no webhook (Phase 8, part 4B):

1. POST /api/public/watch stores a watch with a random code and returns a
   Telegram Start link that carries the code.
2. The visitor taps Start in Telegram, so the bot receives "/start <code>".
3. POST /api/public/watch/confirm reads the latest bot messages once
   (getUpdates), finds the code and saves the chat of the visitor.
4. Part 4C sends the one UP message from the check run.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from typing import Any

from flask import Flask, request

from app import alerts, db, public_check
from app.checker import check_url
from app.security import SecurityError, check_url_target
from app.validation import ValidationError

logger = logging.getLogger(__name__)

BOT_USERNAME = "whydown_nm_bot"

_CODE_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}")
_UPDATES_LIMIT = 50
_UPDATES_MAX_BYTES = 1_000_000


def new_code() -> str:
    """Return a random code that is safe inside a Telegram Start link."""
    return secrets.token_urlsafe(16)


def start_link(code: str) -> str:
    """Return the Telegram link that opens the bot with the Start button."""
    return f"https://t.me/{BOT_USERNAME}?start={code}"


def find_chat_id(updates: list[Any], code: str) -> str | None:
    """Return the chat id of the newest "/start <code>" message, or None."""
    wanted = "/start " + code
    for update in reversed(updates):
        if not isinstance(update, dict):
            continue
        message = update.get("message")
        if not isinstance(message, dict) or message.get("text") != wanted:
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict):
            continue
        chat_id = chat.get("id")
        if chat_id is None or isinstance(chat_id, bool):
            continue
        return str(chat_id)
    return None


def fetch_updates(token: str, timeout: int = alerts.REQUEST_TIMEOUT) -> list[Any]:
    """Read the latest messages sent to the bot (the bot has no webhook).

    The messages are not confirmed, so the call can be repeated. Raises
    TelegramError without the bot token in its message.
    """
    query = urllib.parse.urlencode(
        {"offset": -_UPDATES_LIMIT, "limit": _UPDATES_LIMIT, "timeout": 0}
    )
    url = f"{alerts.TELEGRAM_API_URL}/bot{token}/getUpdates?{query}"
    request_obj = urllib.request.Request(url, method="GET")

    try:
        with urllib.request.urlopen(request_obj, timeout=timeout) as response:
            body = response.read(_UPDATES_MAX_BYTES)
    except urllib.error.HTTPError as exc:
        raise alerts.TelegramError(
            alerts.scrub(f"Telegram answered with HTTP {exc.code}", token)
        ) from None
    except urllib.error.URLError as exc:
        raise alerts.TelegramError(
            alerts.scrub(f"Telegram could not be reached: {exc.reason}", token)
        ) from None
    except OSError as exc:
        raise alerts.TelegramError(
            alerts.scrub(f"Telegram could not be reached: {exc}", token)
        ) from None

    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        raise alerts.TelegramError("The answer of Telegram was not valid JSON") from None
    if not isinstance(data, dict) or not data.get("ok") or not isinstance(data.get("result"), list):
        raise alerts.TelegramError("Telegram did not return the messages")
    return data["result"]


def _text_field(name: str) -> str | None:
    """Return a string field of the JSON body, or None."""
    data = request.get_json(silent=True)
    value = data.get(name) if isinstance(data, dict) else None
    return value if isinstance(value, str) else None


def _close(conn: Any) -> None:
    """Close a connection and ignore any error."""
    if conn is not None:
        with suppress(Exception):
            conn.close()


def register_public_watch_routes(app: Flask) -> None:
    """Register the two public watch endpoints."""

    @app.route("/api/public/watch", methods=["POST", "OPTIONS"])
    def api_public_watch():
        if request.method == "OPTIONS":
            return public_check._cors(app.response_class(status=204))

        url = _text_field("url")
        if url is None:
            return public_check._json({"error": "Send a JSON body with a url."}, 400)
        try:
            normalised = public_check.normalise_url(url)
        except ValidationError:
            return public_check._json(
                {"status": "invalid_url", "message": "Not a valid website address."}, 400
            )

        code = new_code()
        conn = None
        try:
            conn = public_check._open_connection()
            db.create_public_watch(code, normalised, conn=conn)
        except Exception as exc:  # noqa: BLE001 - any failure is a 503
            logger.error("Public watch failed (%s)", type(exc).__name__)
            return public_check._json({"error": "The watch is not available."}, 503)
        finally:
            _close(conn)

        return public_check._json(
            {
                "status": "ok",
                "code": code,
                "link": start_link(code),
                "normalised_url": normalised,
            },
            200,
        )

    @app.route("/api/public/watch/confirm", methods=["POST", "OPTIONS"])
    def api_public_watch_confirm():
        if request.method == "OPTIONS":
            return public_check._cors(app.response_class(status=204))

        code = _text_field("code")
        if code is None or not _CODE_PATTERN.fullmatch(code):
            return public_check._json({"error": "Send a JSON body with a valid code."}, 400)

        credentials = alerts.telegram_credentials()
        if credentials is None:
            return public_check._json({"error": "Telegram is not set up."}, 503)
        token = credentials[0]

        try:
            chat_id = find_chat_id(fetch_updates(token), code)
        except alerts.TelegramError as exc:
            logger.error("Telegram messages failed (%s)", type(exc).__name__)
            return public_check._json({"error": "Telegram is not available."}, 503)

        if chat_id is None:
            return public_check._json(
                {"status": "waiting", "message": "Tap Start in Telegram first, then try again."},
                200,
            )

        conn = None
        try:
            conn = public_check._open_connection()
            watch = db.attach_public_watch_chat(code, chat_id, conn=conn)
        except Exception as exc:  # noqa: BLE001 - any failure is a 503
            logger.error("Public watch failed (%s)", type(exc).__name__)
            return public_check._json({"error": "The watch is not available."}, 503)
        finally:
            _close(conn)

        if watch is None:
            return public_check._json(
                {"status": "expired", "message": "This watch is unknown or has expired."}, 404
            )

        text = (
            f"WhyDown: I am watching {watch['normalised_url']}. "
            "You will get one message here when it is up. "
            f"The watch ends after that message or after {db.WATCH_HOURS} hours."
        )
        with suppress(alerts.TelegramError):
            alerts.send_telegram(text, token, chat_id)

        return public_check._json(
            {"status": "ok", "normalised_url": watch["normalised_url"], "message": text}, 200
        )


# --------------------------------------------------------------------------
# The check run (Phase 8, part 4C)
# --------------------------------------------------------------------------

# A watch is done when the site answers with one of these results.
UP_RESULTS = ("OK", "SLOW", "DNS_SLOW")
# The watches get this many seconds inside one check run (cron-job.org
# gives up after 30 seconds).
WATCH_DEADLINE_SECONDS = 8.0


def up_message(url: str, status_code: Any) -> str:
    """Return the one message a visitor gets when the site is up."""
    code = f" (HTTP {status_code})" if isinstance(status_code, int) else ""
    return (
        f"WhyDown: {url} is up now{code}. "
        "This was your one message and the watch has ended."
    )


def _check_watch(url: str) -> Any:
    """Check the address of a watch. Returns None when the address is blocked."""
    try:
        check_url_target(url)
    except SecurityError:
        return None
    return check_url(url)


def process_public_watches(
    conn: Any,
    deadline: float = WATCH_DEADLINE_SECONDS,
) -> dict[str, int]:
    """Check the active watches and send the UP message for the sites that are up.

    A watch ends after its message, when its address is blocked, or when it is
    older than the watch time. A site that is still down, a check that is not
    ready within ``deadline`` seconds and a failed message keep the watch for
    the next run.
    """
    summary = {"expired": 0, "watched": 0, "sent": 0, "blocked": 0}
    summary["expired"] = db.purge_expired_public_watches(conn=conn)

    watches = db.list_active_public_watches(conn=conn)
    summary["watched"] = len(watches)
    if not watches:
        return summary

    credentials = alerts.telegram_credentials()
    if credentials is None:
        return summary
    token = credentials[0]

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(watches))
    try:
        futures = {
            executor.submit(_check_watch, watch["normalised_url"]): watch
            for watch in watches
        }
        done, _pending = concurrent.futures.wait(futures, timeout=deadline)
        for future in done:
            watch = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - one watch must not stop the others
                logger.error("Public watch check failed (%s)", type(exc).__name__)
                continue

            if result is None:
                db.delete_public_watch(watch["id"], conn=conn)
                summary["blocked"] += 1
                continue
            if getattr(result, "result", None) not in UP_RESULTS:
                continue

            text = up_message(watch["normalised_url"], getattr(result, "status_code", None))
            try:
                alerts.send_telegram(text, token, watch["chat_id"])
            except alerts.TelegramError as exc:
                logger.error("Public watch message failed (%s)", type(exc).__name__)
                continue
            db.delete_public_watch(watch["id"], conn=conn)
            summary["sent"] += 1
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return summary


def process_public_watches_safely() -> None:
    """Run the watches of one check run. Any failure is logged and ignored."""
    conn = None
    try:
        conn = public_check._open_connection()
        process_public_watches(conn)
    except Exception as exc:  # noqa: BLE001 - the check run must never fail because of this
        logger.error("Public watches failed (%s)", type(exc).__name__)
    finally:
        _close(conn)
