"""Tests for the certificate-expiry warning (Phase 4 part C).

No real network and no real database is used: the Telegram call and the two
database calls of the certificate log are replaced by fakes, so every test
runs without touching Telegram or Postgres.

The tests cover the behaviour of the spec: a certificate that reaches a
threshold warns for one tier at a time (T09); a duplicate warning is never
sent (the alert log is the memory, read from the database); several tiers can
be crossed at the same time and only the most urgent one is warned about; a
renewed certificate has another expiry date and warns again; a certificate
that does not expire soon triggers nothing; a broken channel is handled;
missing credentials are skipped; and the run never stops because of a failing
alert.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from app import alerts, runner
from app.checker import CheckResult
from app.config import config

# A fake token: it never talks to Telegram, it only proves that the real token
# never ends up in a log or in an error message.
TOKEN = "123456789:FAKE-TOKEN-abcDEF"
CHAT_ID = "-1001234567890"

# The tiers of the default configuration.
TIERS = (14, 7, 3)


# --------------------------------------------------------------------------
# The fakes
# --------------------------------------------------------------------------

class FakeAlertLog:
    """A recording stand-in for the certificate part of the alert log.

    It answers "was a warning already logged for this certificate and this
    tier" from the rows it collected, exactly like the database does.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.log_error: Exception | None = None
        self.cert_log_error: Exception | None = None

    def alert_exists(self, incident_id, kind, conn=None):
        """Return True when an incident alert of this kind was already logged."""
        return any(
            row["incident_id"] == incident_id and row["kind"] == kind
            for row in self.rows
        )

    def cert_alert_exists(
        self, site_id, cert_expires_at, threshold_days, kind="CERT_WARNING", conn=None
    ):
        """Return True when this certificate was already warned about."""
        return any(
            row["site_id"] == site_id
            and row["cert_expires_at"] == cert_expires_at
            and row["cert_threshold_days"] == int(threshold_days)
            for row in self.rows
        )

    def log_alert(
        self, site_id, incident_id, kind, channel="telegram", success=True, conn=None
    ):
        """Record one attempt of an incident alert."""
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

    def log_cert_alert(
        self,
        site_id,
        cert_expires_at,
        threshold_days,
        kind="CERT_WARNING",
        channel="telegram",
        success=True,
        conn=None,
    ):
        """Record one attempt of a certificate warning."""
        if self.cert_log_error is not None:
            raise self.cert_log_error
        self.rows.append({
            "site_id": site_id,
            "cert_expires_at": cert_expires_at,
            "cert_threshold_days": int(threshold_days),
            "kind": kind,
            "channel": channel,
            "success": success,
        })
        return len(self.rows)

    def cert_rows(self) -> list[dict[str, object]]:
        """The rows of the certificate warnings, in order."""
        return list(self.rows)

    def thresholds(self) -> list[int]:
        """The tiers that were warned about, in order."""
        return [int(row["cert_threshold_days"]) for row in self.rows]


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
    """Replace the certificate-log database calls with the fakes."""
    fake = FakeAlertLog()
    monkeypatch.setattr(alerts, "cert_alert_exists", fake.cert_alert_exists)
    monkeypatch.setattr(alerts, "log_cert_alert", fake.log_cert_alert)
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


@pytest.fixture
def clock(monkeypatch):
    """Freeze the clock of the alert module at a fixed moment."""
    moment = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(alerts, "_now", lambda: moment)
    return moment


# --------------------------------------------------------------------------
# A site and a check with certificate metadata
# --------------------------------------------------------------------------

def _site(site_id: int = 1, name: str = "Portal") -> dict[str, object]:
    return {
        "id": site_id,
        "name": name,
        "url": "https://portal.example.edu/",
        "expected_status": 200,
        "keyword": None,
        "slow_threshold_ms": 5000,
        "enabled": True,
    }


def _cert_result(expires: datetime, expiring_soon: bool = True) -> CheckResult:
    """A check result that carries certificate metadata."""
    result = CheckResult()
    result.result = "OK"
    result.total_ms = 10.0
    result.cert_expiring_soon = expiring_soon
    result.cert_expires_at = expires
    return result


# --------------------------------------------------------------------------
# One warning per threshold, sent once
# --------------------------------------------------------------------------

def test_a_warning_is_sent_when_the_certificate_reaches_a_tier(
    store, telegram, credentials, clock
):
    """A certificate that is exactly at a tier warns for that tier."""
    site = _site()
    expires = clock + timedelta(days=3)

    assert alerts.send_cert_alert(None, site, _cert_result(expires), TIERS) is True

    assert len(telegram.messages) == 1
    message = telegram.messages[0]
    assert "Days left: 3" in message
    assert "Expires:" in message
    assert "portal.example.edu/" in message
    assert store.thresholds() == [3]
    assert store.rows[0]["success"] is True


def test_every_tier_warns_once(store, telegram, credentials, clock):
    """Three certificates at three tiers each warn once (T09)."""
    sent = []

    for days in (3, 7, 14):
        site = _site()
        expires = clock + timedelta(days=days)

        ok = alerts.send_cert_alert(None, site, _cert_result(expires), TIERS)

        assert ok is True
        sent.append(len(telegram.messages))

    # Three distinct warnings, one per tier.
    assert sent == [1, 2, 3]
    assert store.thresholds() == [3, 7, 14]


def test_a_repeated_warning_for_the_same_certificate_is_not_sent(
    store, telegram, credentials, clock
):
    """The alert log stops a second warning for the same certificate and tier."""
    site = _site()
    expires = clock + timedelta(days=3)

    first = alerts.send_cert_alert(None, site, _cert_result(expires), TIERS)
    second = alerts.send_cert_alert(None, site, _cert_result(expires), TIERS)

    assert first is True
    assert second is False
    assert len(telegram.messages) == 1
    assert store.thresholds() == [3]


def test_a_renewed_certificate_restarts_the_cycle(store, telegram, credentials, clock):
    """A renewed certificate has another expiry date and warns again (T09)."""
    site = _site()

    original = clock + timedelta(days=3)
    first = alerts.send_cert_alert(None, site, _cert_result(original), TIERS)

    # The same certificate, checked again: no new warning.
    second = alerts.send_cert_alert(None, site, _cert_result(original), TIERS)

    # A renewed certificate: another expiry date starts the cycle again.
    renewed = clock + timedelta(days=4)
    third = alerts.send_cert_alert(None, site, _cert_result(renewed), TIERS)

    assert first is True
    assert second is False
    assert third is True
    assert len(telegram.messages) == 2
    assert [row["cert_expires_at"] for row in store.cert_rows()] == [original, renewed]


def test_the_warning_is_remembered_in_the_database_not_in_the_process(
    store, telegram, credentials, clock
):
    """The duplicate check asks the database, not a variable of the process."""
    site = _site()
    expires = clock + timedelta(days=3)

    alerts.send_cert_alert(None, site, _cert_result(expires), TIERS)

    assert store.rows == [{
        "site_id": 1,
        "cert_expires_at": expires,
        "cert_threshold_days": 3,
        "kind": alerts.KIND_CERT_WARNING,
        "channel": "telegram",
        "success": True,
    }]


# --------------------------------------------------------------------------
# Only the most urgent tier fires when several are crossed at once
# --------------------------------------------------------------------------

def test_only_the_lowest_crossed_tier_is_sent(store, telegram, credentials, clock):
    """Five days left crosses the 14-day and the 7-day tier: only 7 warns."""
    site = _site()
    expires = clock + timedelta(days=5)

    assert alerts.send_cert_alert(None, site, _cert_result(expires), TIERS) is True

    assert len(telegram.messages) == 1
    assert store.thresholds() == [7]


def test_the_most_urgent_tier_wins_when_all_tiers_are_crossed(
    store, telegram, credentials, clock
):
    """Two days left crosses every tier: only the smallest one warns."""
    site = _site()
    expires = clock + timedelta(days=2)

    assert alerts.send_cert_alert(None, site, _cert_result(expires), TIERS) is True

    assert store.thresholds() == [3]


def test_no_tier_is_reached_before_the_widest_one(store, telegram, credentials, clock):
    """A certificate that is far away triggers nothing."""
    site = _site()
    expires = clock + timedelta(days=30)

    assert alerts.send_cert_alert(None, site, _cert_result(expires), TIERS) is False

    assert telegram.messages == []
    assert store.rows == []


# --------------------------------------------------------------------------
# The rounding of the remaining days
# --------------------------------------------------------------------------

def test_days_left_rounds_up_so_a_fractional_day_matches_a_tier(
    store, telegram, credentials, monkeypatch
):
    """13 days and 23 hours left are 14 days left, so the 14-day tier fires."""
    moment = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(alerts, "_now", lambda: moment)
    site = _site()
    expires = moment + timedelta(days=13, hours=23)

    assert alerts._days_left(expires) == 14
    assert alerts.send_cert_alert(None, site, _cert_result(expires), TIERS) is True
    assert store.thresholds() == [14]


def test_an_expired_certificate_is_not_warned(store, telegram, credentials, clock):
    """An expired certificate is reported by the check itself (CERT_EXPIRED)."""
    site = _site()
    expires = clock - timedelta(days=1)

    assert alerts._days_left(expires) is None
    assert alerts.send_cert_alert(None, site, _cert_result(expires), TIERS) is False
    assert telegram.messages == []
    assert store.rows == []


# --------------------------------------------------------------------------
# Nothing happens when the certificate does not expire soon
# --------------------------------------------------------------------------

def test_a_check_that_does_not_expire_soon_warns_nothing(
    store, telegram, credentials, clock
):
    """The flag of the checker is false, so no warning is sent."""
    site = _site()
    result = _cert_result(clock + timedelta(days=100), expiring_soon=False)

    assert alerts.send_cert_alert(None, site, result, TIERS) is False
    assert telegram.messages == []
    assert store.rows == []


def test_a_check_without_an_expiry_date_warns_nothing(
    store, telegram, credentials, clock
):
    """Without an expiry date there is nothing to warn about."""
    site = _site()
    result = _cert_result(None)

    assert alerts.send_cert_alert(None, site, result, TIERS) is False
    assert telegram.messages == []
    assert store.rows == []


# --------------------------------------------------------------------------
# Missing credentials are skipped quietly
# --------------------------------------------------------------------------

def test_without_a_token_the_warning_is_skipped(store, telegram, no_credentials, clock):
    """Without a bot token the warning is skipped and the run continues."""
    site = _site()
    expires = clock + timedelta(days=3)

    assert alerts.send_cert_alert(None, site, _cert_result(expires), TIERS) is False
    assert telegram.messages == []
    assert store.rows == []


def test_without_a_chat_id_the_warning_is_skipped(
    store, telegram, monkeypatch, clock
):
    """Without a chat id the warning is skipped quietly."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    site = _site()
    expires = clock + timedelta(days=3)

    assert alerts.send_cert_alert(None, site, _cert_result(expires), TIERS) is False
    assert telegram.messages == []
    assert store.rows == []


# --------------------------------------------------------------------------
# A broken channel never breaks the run
# --------------------------------------------------------------------------

def test_a_failing_telegram_call_returns_false(store, telegram, credentials, clock):
    """A network error becomes a failed warning, not a raised exception."""
    telegram.error = RuntimeError("the network is down")

    site = _site()
    expires = clock + timedelta(days=3)

    assert alerts.send_cert_alert(None, site, _cert_result(expires), TIERS) is False
    assert telegram.messages == []  # the message never arrived
    assert store.rows[0]["success"] is False


def test_a_failing_certificate_log_does_not_stop_the_warning(
    store, telegram, credentials, clock
):
    """A broken database log is caught: the warning was sent anyway."""
    store.cert_log_error = RuntimeError("the database is gone")

    site = _site()
    expires = clock + timedelta(days=3)

    assert alerts.send_cert_alert(None, site, _cert_result(expires), TIERS) is True
    assert len(telegram.messages) == 1


def test_an_unexpected_error_inside_the_warning_does_not_raise(
    store, telegram, credentials, monkeypatch, clock
):
    """Even an unexpected error is caught by ``send_cert_alert``."""
    def broken_exists(*args, **kwargs):
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(alerts, "cert_alert_exists", broken_exists)

    site = _site()
    expires = clock + timedelta(days=3)

    assert alerts.send_cert_alert(None, site, _cert_result(expires), TIERS) is False


def test_the_token_never_reaches_the_log(store, telegram, credentials, clock, caplog):
    """The text of an error is scrubbed before it is logged."""
    telegram.error = RuntimeError(
        f"failed to reach https://api.telegram.org/bot{TOKEN}/sendMessage"
    )

    site = _site()
    expires = clock + timedelta(days=3)

    with caplog.at_level(logging.DEBUG):
        alerts.send_cert_alert(None, site, _cert_result(expires), TIERS)

    assert TOKEN not in caplog.text


# --------------------------------------------------------------------------
# The message
# --------------------------------------------------------------------------

def test_the_cert_message_is_plain_text_and_names_metadata(
    store, telegram, credentials, clock
):
    """The warning names the site, its URL, its expiry and the days left."""
    site = _site()

    alerts.send_cert_alert(None, site, _cert_result(clock + timedelta(days=5)), TIERS)

    message = telegram.messages[0]
    assert message.startswith(
        "WhyDown: the certificate of a monitored site is about to expire\n"
    )
    assert "Portal" in message
    assert "https://portal.example.edu/" in message
    assert "Expires:" in message
    assert "Days left: 5" in message
    # Plain text: no markup that Telegram would render.
    assert "<" not in message
    assert "{" not in message


# --------------------------------------------------------------------------
# Low level helpers
# --------------------------------------------------------------------------

def test_crossed_thresholds_returns_ascending_tiers():
    """The tiers that are reached are returned in ascending order."""
    assert alerts._crossed_thresholds(5, [3, 7, 14]) == [7, 14]
    assert alerts._crossed_thresholds(14, [3, 7, 14]) == [14]
    assert alerts._crossed_thresholds(30, [3, 7, 14]) == []


def test_normalize_thresholds_sorts_and_filters():
    """Empty, zero and negative values are ignored; the result is sorted."""
    assert alerts._normalize_thresholds(None) == []
    assert alerts._normalize_thresholds([]) == []
    assert alerts._normalize_thresholds(False) == []
    assert alerts._normalize_thresholds((14, 7, 3)) == [3, 7, 14]
    assert alerts._normalize_thresholds("14,7,3") == [3, 7, 14]
    assert alerts._normalize_thresholds("14 , 7 , 3 , 0") == [3, 7, 14]
    assert alerts._normalize_thresholds(["14", "not a number", 3]) == [3, 14]


def test_normalize_thresholds_accepts_a_single_number():
    """A configuration with one tier is a number, not a list."""
    assert alerts._normalize_thresholds(7) == [7]
    assert alerts._normalize_thresholds(7.9) == [7]


def test_normalize_thresholds_ignores_empty_and_broken_parts():
    """A broken part of the string does not stop the other tiers."""
    assert alerts._normalize_thresholds("14,,7") == [7, 14]
    assert alerts._normalize_thresholds("14,seven,7") == [7, 14]


def test_days_left_of_an_unreadable_expiry_is_none():
    """An expiry that cannot be read has no days left."""
    assert alerts._days_left(None) is None
    assert alerts._days_left("not a timestamp") is None


def test_the_clock_of_the_alert_module_is_the_current_time():
    """The seam returns the current time when it is not replaced."""
    before = datetime.now(UTC)
    now = alerts._now()
    assert before <= now <= datetime.now(UTC)


def test_days_left_ignores_a_naive_expiry(monkeypatch):
    """A naive expiry gets a UTC timezone before the comparison."""
    moment = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(alerts, "_now", lambda: moment)

    assert alerts._days_left(datetime(2026, 10, 25, 0, 0, 0)) == 3


def test_the_runner_uses_the_tiers_of_the_configuration():
    """The tiers of the configuration are the ones the runner hands over."""
    assert isinstance(config.thresholds.cert_alert_thresholds, tuple)
    assert config.thresholds.cert_alert_thresholds == tuple(
        sorted(config.thresholds.cert_alert_thresholds)
    )


# --------------------------------------------------------------------------
# The warning inside the runner
# --------------------------------------------------------------------------

class FakeConnection:
    """A stand-in for a psycopg connection; the runner only closes it."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _run_with_cert_alert(results, send_cert_alert_func, sites=None):
    """Run the runner with fake database functions and a fake warning sender."""
    sites = sites or [_site(1)]
    results = dict(results)
    saved: list[int] = []

    def check_url(url, **kwargs):
        return results[url]

    def save_check(site_id, result, conn=None):
        saved.append(site_id)
        return 1

    summary = runner.run_all_checks(
        list_sites_func=lambda conn=None, enabled_only=False: list(sites),
        check_url_func=check_url,
        save_check_func=save_check,
        purge_func=lambda days=30, conn=None: 0,
        process_check_func=lambda conn, site, result: None,
        send_alert_func=lambda conn, site, incident, kind: True,
        send_cert_alert_func=send_cert_alert_func,
        get_incident_func=lambda site_id, conn=None: None,
        connect_func=FakeConnection,
        max_workers=1,
        site_timeout_seconds=5,
        run_deadline_seconds=20,
    )
    return summary, saved


def test_the_runner_warns_after_a_saved_check(store, telegram, credentials, clock):
    """A stored check with an expiring certificate sends the warning."""
    calls: list[tuple[int, object]] = []

    def send_cert_alert(conn, site, result, threshold_days):
        calls.append((site["id"], threshold_days))
        return True

    site = _site(4)
    summary, saved = _run_with_cert_alert(
        {"https://portal.example.edu/": _cert_result(clock + timedelta(days=3))},
        send_cert_alert,
        sites=[site],
    )

    assert summary["sites_checked"] == 1
    assert saved == [4]
    assert calls == [(4, config.thresholds.cert_alert_thresholds)]


def test_the_runner_does_not_warn_when_nothing_expires(
    store, telegram, credentials, clock
):
    """A check without the flag sends no warning."""
    calls: list[tuple[int, object]] = []

    def send_cert_alert(conn, site, result, threshold_days):
        calls.append((site["id"], threshold_days))
        return True

    result = _cert_result(clock + timedelta(days=100), expiring_soon=False)
    _run_with_cert_alert({"https://portal.example.edu/": result}, send_cert_alert)

    assert calls == []


def test_a_failing_warning_does_not_stop_the_run(store, telegram, credentials, clock):
    """A broken alert channel never stops the other sites (spec section 10)."""
    def failing_send(conn, site, result, threshold_days):
        raise RuntimeError("the alert channel is down")

    site = _site(1)
    summary, saved = _run_with_cert_alert(
        {
            "https://portal.example.edu/": _cert_result(clock + timedelta(days=3)),
        },
        failing_send,
        sites=[site, _site(2, "Second")],
    )

    assert summary["sites_checked"] == 2
    assert summary["counts"] == {"OK": 2}
    # The sites are checked concurrently, so the order of the saves is not fixed.
    assert sorted(saved) == [1, 2]


def test_the_runner_warns_with_the_same_connection_as_the_check(
    store, telegram, credentials, clock
):
    """The warning uses the connection of the run."""
    connections: list[object] = []

    def send_cert_alert(conn, site, result, threshold_days):
        connections.append(conn)
        return True

    _run_with_cert_alert(
        {"https://portal.example.edu/": _cert_result(clock + timedelta(days=3))},
        send_cert_alert,
    )

    assert connections and connections[0] is not None


# --------------------------------------------------------------------------
# Edge cases inside the certificate handler of the module
# --------------------------------------------------------------------------

def test_a_site_without_an_id_is_skipped(store, telegram, credentials, clock):
    """A site that cannot be identified is not warned about."""
    assert alerts.send_cert_alert(
        None, {"name": "x", "url": "y"}, _cert_result(clock + timedelta(days=3)), TIERS
    ) is False
    assert telegram.messages == []
    assert store.rows == []


def test_an_empty_tier_list_is_skipped(store, telegram, credentials, clock):
    """No warning tier means no warning can fire (always skipped)."""
    site = _site()
    assert alerts.send_cert_alert(
        None, site, _cert_result(clock + timedelta(days=3)), []
    ) is False
    assert telegram.messages == []
    assert store.rows == []


def test_the_default_tiers_from_the_configuration_fire(
    store, telegram, credentials, clock
):
    """Without an explicit tier list the configured tiers are used."""
    site = _site()
    tier = min(config.thresholds.cert_alert_thresholds)  # the smallest tier
    expires = clock + timedelta(days=tier)

    assert alerts.send_cert_alert(None, site, _cert_result(expires)) is True
    assert len(telegram.messages) == 1
    assert store.thresholds() == [tier]
