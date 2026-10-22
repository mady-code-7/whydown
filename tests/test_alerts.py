"""Tests for the alert sending (Phase 4 part B).

No real network and no real database is used: the Telegram call and the two
database calls of the alert log are replaced by fakes, so every test runs
without touching Telegram or Postgres.

The tests cover the behaviour of the spec: a DOWN alert and a RECOVERY
alert are sent exactly once per incident (T11, T12, T13), a missing
configuration is skipped quietly (T19), a broken alert channel never breaks
a run, and the bot token never reaches a log or an error message
(spec section 12).
"""

from __future__ import annotations

import json
import logging
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from app import alerts

# A fake token: it never talks to Telegram, it only proves that the real
# token never ends up in a log or in an error message.
TOKEN = "123456789:FAKE-TOKEN-abcDEF"
CHAT_ID = "-1001234567890"


# --------------------------------------------------------------------------
# The fakes
# --------------------------------------------------------------------------

class FakeAlertLog:
    """A recording stand-in for the two database calls of the alert log.

    It answers "was an alert of this kind already logged for this incident"
    from the rows it has collected, exactly like the database does.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.log_error: Exception | None = None

    def alert_exists(self, incident_id, kind, conn=None):
        """Return True when an alert of this kind was already logged."""
        return any(
            row["incident_id"] == incident_id and row["kind"] == kind
            for row in self.rows
        )

    def log_alert(self, site_id, incident_id, kind, channel="telegram", success=True, conn=None):
        """Record one attempt."""
        if self.log_error is not None:
            raise self.log_error
        self.rows.append({
            "site_id": site_id,
            "incident_id": incident_id,
            "kind": kind,
            "channel": channel,
            "success": success,
        })
        return len(self.rows)

    def kinds(self) -> list[str]:
        """The kinds of the logged attempts, in order."""
        return [str(row["kind"]) for row in self.rows]


class FakeTelegram:
    """A recording stand-in for the Telegram call."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.error: Exception | None = None

    def send(self, text, token, chat_id, timeout=alerts.REQUEST_TIMEOUT):
        """Record one message, or fail like a broken channel."""
        if self.error is not None:
            raise self.error
        self.messages.append(text)


@pytest.fixture
def store(monkeypatch):
    """Replace the two database calls of the alert log."""
    fake = FakeAlertLog()
    monkeypatch.setattr(alerts, "alert_exists", fake.alert_exists)
    monkeypatch.setattr(alerts, "log_alert", fake.log_alert)
    return fake


@pytest.fixture
def telegram(monkeypatch):
    """Replace the Telegram call."""
    fake = FakeTelegram()
    monkeypatch.setattr(alerts, "send_telegram", fake.send)
    return fake


@pytest.fixture
def credentials(monkeypatch):
    """Put a fake Telegram configuration into the environment."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)


@pytest.fixture
def no_credentials(monkeypatch):
    """Remove the Telegram configuration from the environment."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


def _site() -> dict[str, object]:
    return {"id": 3, "name": "College portal", "url": "https://portal.example.edu/"}


def _down_incident() -> dict[str, object]:
    return {
        "id": 7,
        "site_id": 3,
        "started_at": datetime(2026, 10, 22, 10, 0, 0, tzinfo=UTC),
        "ended_at": None,
        "failure_type": "HTTP_5XX",
    }


def _closed_incident() -> dict[str, object]:
    started = datetime(2026, 10, 22, 10, 0, 0, tzinfo=UTC)
    return {
        "id": 7,
        "site_id": 3,
        "started_at": started,
        "ended_at": started + timedelta(minutes=12, seconds=30),
        "failure_type": "HTTP_5XX",
    }


# --------------------------------------------------------------------------
# The DOWN alert
# --------------------------------------------------------------------------

def test_a_down_alert_is_sent(store, telegram, credentials):
    """An opened incident sends one DOWN alert."""
    assert alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN) is True

    assert len(telegram.messages) == 1


def test_the_down_alert_names_the_site_and_its_url(store, telegram, credentials):
    """The message holds the name and the URL of the site."""
    alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN)

    message = telegram.messages[0]
    assert "College portal" in message
    assert "https://portal.example.edu/" in message


def test_the_down_alert_names_the_failure_type(store, telegram, credentials):
    """The message holds the classification of the failure."""
    alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN)

    assert "HTTP_5XX" in telegram.messages[0]


def test_the_down_alert_names_the_start_of_the_incident(store, telegram, credentials):
    """The message holds the time the incident started."""
    alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN)

    assert "2026-10-22 10:00:00 UTC" in telegram.messages[0]


def test_the_down_alert_is_plain_text(store, telegram, credentials):
    """The message is plain text without any markup."""
    alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN)

    message = telegram.messages[0]
    assert "<" not in message
    assert "{" not in message


def test_the_down_alert_is_logged(store, telegram, credentials):
    """The attempt is written to the alert log."""
    alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN)

    assert len(store.rows) == 1
    row = store.rows[0]
    assert row["kind"] == alerts.KIND_DOWN
    assert row["site_id"] == 3
    assert row["incident_id"] == 7
    assert row["channel"] == "telegram"
    assert row["success"] is True


# --------------------------------------------------------------------------
# The RECOVERY alert
# --------------------------------------------------------------------------

def test_a_recovery_alert_is_sent(store, telegram, credentials):
    """A closed incident sends one RECOVERY alert."""
    assert alerts.send_alert(None, _site(), _closed_incident(), alerts.KIND_RECOVERY) is True

    assert len(telegram.messages) == 1


def test_the_recovery_alert_names_the_site_and_its_url(store, telegram, credentials):
    """The message holds the name and the URL of the site."""
    alerts.send_alert(None, _site(), _closed_incident(), alerts.KIND_RECOVERY)

    message = telegram.messages[0]
    assert "College portal" in message
    assert "https://portal.example.edu/" in message


def test_the_recovery_alert_names_the_downtime(store, telegram, credentials):
    """The message holds how long the site was down."""
    alerts.send_alert(None, _site(), _closed_incident(), alerts.KIND_RECOVERY)

    assert "12m 30s" in telegram.messages[0]


def test_the_recovery_alert_is_logged(store, telegram, credentials):
    """The attempt is written to the alert log."""
    alerts.send_alert(None, _site(), _closed_incident(), alerts.KIND_RECOVERY)

    assert store.kinds() == [alerts.KIND_RECOVERY]
    assert store.rows[0]["success"] is True


def test_the_recovery_alert_of_an_open_incident_has_no_duration(store, telegram, credentials):
    """An incident without an end has no duration yet."""
    alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_RECOVERY)

    assert "unknown" in telegram.messages[0]


# --------------------------------------------------------------------------
# Exactly one alert per incident
# --------------------------------------------------------------------------

def test_a_second_down_alert_for_the_same_incident_is_not_sent(store, telegram, credentials):
    """The alert log stops a second DOWN alert of the same incident (T12)."""
    alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN)
    alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN)

    assert len(telegram.messages) == 1


def test_a_second_recovery_alert_for_the_same_incident_is_not_sent(store, telegram, credentials):
    """The alert log stops a second RECOVERY alert of the same incident."""
    alerts.send_alert(None, _site(), _closed_incident(), alerts.KIND_RECOVERY)
    alerts.send_alert(None, _site(), _closed_incident(), alerts.KIND_RECOVERY)

    assert len(telegram.messages) == 1


def test_the_alert_log_is_the_memory_of_the_sent_alerts(store, telegram, credentials):
    """The duplicate check asks the database, not a variable of the process."""
    alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN)

    assert store.rows == [{
        "site_id": 3,
        "incident_id": 7,
        "kind": alerts.KIND_DOWN,
        "channel": "telegram",
        "success": True,
    }]


def test_a_second_incident_gets_its_own_down_alert(store, telegram, credentials):
    """The alert of a new incident is not blocked by the alert of the old one."""
    first = _down_incident()
    second = dict(first, id=8)

    alerts.send_alert(None, _site(), first, alerts.KIND_DOWN)
    alerts.send_alert(None, _site(), second, alerts.KIND_DOWN)

    assert len(telegram.messages) == 2


def test_a_recovery_alert_is_sent_after_the_down_alert(store, telegram, credentials):
    """The two alerts of one incident do not block each other."""
    alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN)
    alerts.send_alert(None, _site(), _closed_incident(), alerts.KIND_RECOVERY)

    assert len(telegram.messages) == 2
    assert store.kinds() == [alerts.KIND_DOWN, alerts.KIND_RECOVERY]


# --------------------------------------------------------------------------
# A missing configuration
# --------------------------------------------------------------------------

def test_without_a_token_the_alert_is_skipped(store, telegram, no_credentials):
    """Without a bot token the alert is skipped quietly."""
    assert alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN) is False

    assert telegram.messages == []


def test_without_a_chat_id_the_alert_is_skipped(store, telegram, monkeypatch):
    """Without a chat id the alert is skipped quietly."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    assert alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN) is False

    assert telegram.messages == []


def test_a_skipped_alert_is_not_logged(store, telegram, no_credentials):
    """Nothing was sent, so nothing is written to the alert log."""
    alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN)

    assert store.rows == []


# --------------------------------------------------------------------------
# A broken channel
# --------------------------------------------------------------------------

def test_a_failing_telegram_call_does_not_raise(store, telegram, credentials):
    """A network error is caught and reported as a failed alert."""
    telegram.error = RuntimeError("the network is down")

    assert alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN) is False


def test_a_failing_telegram_call_is_logged_as_a_failed_attempt(store, telegram, credentials):
    """The failed attempt is written to the alert log."""
    telegram.error = RuntimeError("the network is down")

    alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN)

    assert store.rows[0]["success"] is False


def test_a_failing_alert_log_does_not_raise(store, telegram, credentials):
    """A broken database does not break the alert."""
    store.log_error = RuntimeError("the database is gone")

    assert alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN) is True


def test_an_unknown_kind_is_skipped(store, telegram, credentials):
    """Only a DOWN and a RECOVERY alert are sent in this phase."""
    assert alerts.send_alert(None, _site(), _down_incident(), "CERT_WARNING") is False

    assert telegram.messages == []


def test_an_incident_without_an_id_is_skipped(store, telegram, credentials):
    """An incident that cannot be identified is not sent."""
    incident = dict(_down_incident())
    incident.pop("id")

    assert alerts.send_alert(None, _site(), incident, alerts.KIND_DOWN) is False

    assert telegram.messages == []


# --------------------------------------------------------------------------
# The token never leaks
# --------------------------------------------------------------------------

def test_the_token_never_reaches_the_log(store, telegram, credentials, caplog):
    """The text of an error is scrubbed before it is logged."""
    telegram.error = RuntimeError(f"failed to reach https://api.telegram.org/bot{TOKEN}/sendMessage")

    with caplog.at_level(logging.DEBUG):
        alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN)

    assert TOKEN not in caplog.text


def test_the_token_is_scrubbed_from_a_text():
    """The scrubber removes the token from any text."""
    text = f"https://api.telegram.org/bot{TOKEN}/sendMessage failed"

    scrubbed = alerts.scrub(text, TOKEN)

    assert TOKEN not in scrubbed
    assert "<redacted>" in scrubbed


def test_the_scrubber_leaves_a_text_without_a_token_alone():
    """A text without a token is not changed."""
    assert alerts.scrub("nothing to hide", TOKEN) == "nothing to hide"


# --------------------------------------------------------------------------
# The Telegram call itself
# --------------------------------------------------------------------------

class FakeResponse:
    """A stand-in for the answer of the Telegram API."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def read(self, limit: int = -1) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def test_send_telegram_posts_to_the_bot_api(monkeypatch):
    """The message is posted to the sendMessage endpoint of the bot API."""
    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["payload"] = request.data
        seen["timeout"] = timeout
        return FakeResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)

    alerts.send_telegram("hello", TOKEN, CHAT_ID)

    assert seen["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert seen["timeout"] == alerts.REQUEST_TIMEOUT
    assert json.loads(seen["payload"]) == {"chat_id": CHAT_ID, "text": "hello"}


def test_send_telegram_reports_a_refused_message(monkeypatch):
    """Telegram answers with ok=false when it refuses a message."""
    def fake_urlopen(request, timeout=None):
        return FakeResponse(json.dumps({"ok": False, "description": "chat not found"}).encode())

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(alerts.TelegramError) as error:
        alerts.send_telegram("hello", TOKEN, CHAT_ID)

    assert "chat not found" in str(error.value)


def test_send_telegram_reports_an_http_error_without_the_token(monkeypatch):
    """An HTTP error becomes a TelegramError without the token."""
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 404, "Not Found", {}, None
        )

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(alerts.TelegramError) as error:
        alerts.send_telegram("hello", TOKEN, CHAT_ID)

    assert TOKEN not in str(error.value)
    assert "404" in str(error.value)


def test_send_telegram_reports_an_unreachable_api_without_the_token(monkeypatch):
    """A network error becomes a TelegramError without the token."""
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(f"could not reach {request.full_url}")

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(alerts.TelegramError) as error:
        alerts.send_telegram("hello", TOKEN, CHAT_ID)

    assert TOKEN not in str(error.value)


def test_send_telegram_reports_an_answer_that_is_not_json(monkeypatch):
    """An answer that is not JSON is a failure, not a silent success."""
    def fake_urlopen(request, timeout=None):
        return FakeResponse(b"<html>not the api</html>")

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(alerts.TelegramError):
        alerts.send_telegram("hello", TOKEN, CHAT_ID)


def test_send_telegram_reports_an_answer_that_is_not_an_object(monkeypatch):
    """An answer that is not a JSON object is a failure as well."""
    def fake_urlopen(request, timeout=None):
        return FakeResponse(b"[1, 2, 3]")

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(alerts.TelegramError):
        alerts.send_telegram("hello", TOKEN, CHAT_ID)


def test_send_telegram_reports_a_broken_connection(monkeypatch):
    """A connection that breaks is a failure, not a silent success."""
    def fake_urlopen(request, timeout=None):
        raise OSError("the connection was reset")

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(alerts.TelegramError) as error:
        alerts.send_telegram("hello", TOKEN, CHAT_ID)

    assert TOKEN not in str(error.value)


def test_send_telegram_reports_a_failing_status(monkeypatch):
    """An answer with a failing status is a failure."""
    def fake_urlopen(request, timeout=None):
        return FakeResponse(b"{}", status=500)

    monkeypatch.setattr(alerts.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(alerts.TelegramError) as error:
        alerts.send_telegram("hello", TOKEN, CHAT_ID)

    assert "500" in str(error.value)


# --------------------------------------------------------------------------
# The helpers of the message
# --------------------------------------------------------------------------

def test_an_empty_text_is_not_scrubbed():
    """An empty text stays empty."""
    assert alerts.scrub("", TOKEN) == ""


def test_the_scrubber_reads_the_token_from_the_environment(monkeypatch):
    """The scrubber uses the token of the environment when it gets none."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)

    scrubbed = alerts.scrub(f"a message with {TOKEN} in it")

    assert TOKEN not in scrubbed


def test_a_site_may_be_an_object_with_attributes(store, telegram, credentials):
    """The site may also be an object with attributes."""
    site = type("Site", (), {"id": 3, "name": "College portal", "url": "https://x.test/"})()

    assert alerts.send_alert(None, site, _down_incident(), alerts.KIND_DOWN) is True

    assert "College portal" in telegram.messages[0]


def test_a_site_without_an_id_is_skipped(store, telegram, credentials):
    """A site that cannot be identified is not sent."""
    assert alerts.send_alert(None, {"name": "x", "url": "y"}, _down_incident(), alerts.KIND_DOWN) is False

    assert telegram.messages == []


def test_a_site_with_an_unusable_id_is_skipped(store, telegram, credentials):
    """A site whose id is not a number is not sent."""
    site = {"id": "not-a-number", "name": "x", "url": "y"}

    assert alerts.send_alert(None, site, _down_incident(), alerts.KIND_DOWN) is False

    assert telegram.messages == []


def test_a_missing_site_is_skipped(store, telegram, credentials):
    """A site that is not there at all is not sent."""
    assert alerts.send_alert(None, None, _down_incident(), alerts.KIND_DOWN) is False

    assert telegram.messages == []


def test_an_incident_with_an_unusable_id_is_skipped(store, telegram, credentials):
    """An incident whose id is not a number is not sent."""
    incident = dict(_down_incident(), id="not-a-number")

    assert alerts.send_alert(None, _site(), incident, alerts.KIND_DOWN) is False

    assert telegram.messages == []


def test_a_timestamp_may_be_a_string():
    """A timestamp that is stored as a string is read as well."""
    assert alerts.format_timestamp("2026-10-22T10:00:00+00:00") == "2026-10-22 10:00:00 UTC"


def test_a_timestamp_that_is_not_a_timestamp_is_unknown():
    """A broken timestamp is reported as unknown."""
    assert alerts.format_timestamp("not a timestamp") == "unknown"
    assert alerts.format_timestamp(None) == "unknown"


def test_a_timestamp_without_a_timezone_has_no_utc_marker():
    """A timestamp without a timezone is printed as it is."""
    assert alerts.format_timestamp(datetime(2026, 10, 22, 10, 0, 0)) == "2026-10-22 10:00:00"


def test_a_long_downtime_is_formatted_with_hours():
    """A downtime of more than an hour names the hours."""
    started = datetime(2026, 10, 22, 10, 0, 0, tzinfo=UTC)
    incident = {
        "started_at": started,
        "ended_at": started + timedelta(hours=2, minutes=5, seconds=3),
    }

    assert alerts.format_duration(incident) == "2h 5m 3s"


def test_a_short_downtime_is_formatted_in_seconds():
    """A downtime below a minute is formatted in seconds."""
    started = datetime(2026, 10, 22, 10, 0, 0, tzinfo=UTC)
    incident = {"started_at": started, "ended_at": started + timedelta(seconds=42)}

    assert alerts.format_duration(incident) == "42s"


def test_a_downtime_that_starts_after_it_ends_is_zero():
    """A broken duration never becomes a negative one."""
    started = datetime(2026, 10, 22, 10, 0, 0, tzinfo=UTC)
    incident = {"started_at": started, "ended_at": started - timedelta(minutes=5)}

    assert alerts.format_duration(incident) == "0s"


def test_a_duration_without_a_start_is_unknown():
    """An incident without a start has no duration."""
    assert alerts.format_duration({"started_at": None, "ended_at": None}) == "unknown"


# --------------------------------------------------------------------------
# An unexpected error
# --------------------------------------------------------------------------

def test_an_unexpected_error_does_not_raise(store, telegram, credentials, monkeypatch):
    """Even an unexpected error inside the alert is caught."""
    def broken_exists(incident_id, kind, conn=None):
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(alerts, "alert_exists", broken_exists)

    assert alerts.send_alert(None, _site(), _down_incident(), alerts.KIND_DOWN) is False


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------

def test_the_test_command_sends_a_test_message(monkeypatch, telegram, credentials, capsys):
    """``python -m app.alerts test`` sends one test message."""
    monkeypatch.setattr(alerts, "send_telegram", telegram.send)

    assert alerts.main(["test"]) == 0

    assert telegram.messages == ["WhyDown test alert"]
    assert "OK" in capsys.readouterr().out


def test_the_test_command_reports_a_failure(monkeypatch, telegram, credentials, capsys):
    """A broken channel is reported with a scrubbed error."""
    telegram.error = RuntimeError(f"failed to reach https://api.telegram.org/bot{TOKEN}/sendMessage")
    monkeypatch.setattr(alerts, "send_telegram", telegram.send)

    assert alerts.main(["test"]) == 1

    printed = capsys.readouterr().err
    assert TOKEN not in printed
    assert "Test alert failed" in printed


def test_the_test_command_without_credentials(monkeypatch, no_credentials, capsys):
    """Without a configuration the command explains what is missing."""
    assert alerts.main(["test"]) == 1

    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_an_unknown_command_is_reported(capsys):
    """An unknown command is an error."""
    assert alerts.main(["nonsense"]) == 2

    assert "unknown command" in capsys.readouterr().err


def test_without_a_command_the_usage_is_printed(capsys):
    """The usage is printed when no command is given."""
    assert alerts.main([]) == 1

    assert "usage" in capsys.readouterr().out


def test_the_help_is_printed(capsys):
    """``--help`` prints the usage."""
    assert alerts.main(["--help"]) == 0

    assert "usage" in capsys.readouterr().out


def test_the_command_is_read_from_the_arguments_of_the_process(monkeypatch, telegram, credentials):
    """Without arguments the command line of the process is used."""
    monkeypatch.setattr("sys.argv", ["alerts", "test"])

    assert alerts.main() == 0

    assert telegram.messages == ["WhyDown test alert"]
