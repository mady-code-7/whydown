"""Tests for the check run of the public watches (Phase 8, part 4C).

No real database, no Telegram and no network access is used.
"""

from __future__ import annotations

import time

from app import alerts, create_app, db, public_check, public_watch, routes
from app.checker import CheckResult
from app.security import SecurityError


def _result(code, status_code=200):
    result = CheckResult()
    result.result = code
    result.status_code = status_code
    return result


def _watch(watch_id, url, chat_id="111"):
    return {"id": watch_id, "normalised_url": url, "chat_id": chat_id}


def _setup(monkeypatch, watches, results, deleted, sent, expired=0, credentials=("tok", "999")):
    monkeypatch.setattr(db, "purge_expired_public_watches", lambda conn=None: expired)
    monkeypatch.setattr(db, "list_active_public_watches", lambda limit=10, conn=None: watches)

    def fake_delete(watch_id, conn=None):
        deleted.append(watch_id)
        return True

    def fake_send(text, token, chat_id, timeout=10):
        sent.append((text, token, chat_id))

    monkeypatch.setattr(db, "delete_public_watch", fake_delete)
    monkeypatch.setattr(alerts, "telegram_credentials", lambda: credentials)
    monkeypatch.setattr(alerts, "send_telegram", fake_send)
    monkeypatch.setattr(public_watch, "_check_watch", lambda url: results[url])


def test_up_message_has_the_address_and_the_status_code():
    text = public_watch.up_message("https://a.test/", 200)

    assert "https://a.test/" in text
    assert "(HTTP 200)" in text
    assert "one message" in text


def test_up_message_without_a_status_code_has_no_http_part():
    text = public_watch.up_message("https://a.test/", None)

    assert "HTTP" not in text
    assert "https://a.test/" in text


def test_process_sends_one_message_and_deletes_the_watch_when_the_site_is_up(monkeypatch):
    deleted, sent = [], []
    watches = [_watch(1, "https://a.test/", "111")]
    _setup(monkeypatch, watches, {"https://a.test/": _result("OK")}, deleted, sent)

    summary = public_watch.process_public_watches(object())

    assert summary == {"expired": 0, "watched": 1, "sent": 1, "blocked": 0}
    assert len(sent) == 1
    assert sent[0][1] == "tok"
    assert sent[0][2] == "111"
    assert "https://a.test/" in sent[0][0]
    assert deleted == [1]


def test_process_keeps_the_watch_while_the_site_is_down(monkeypatch):
    deleted, sent = [], []
    watches = [_watch(1, "https://a.test/")]
    _setup(monkeypatch, watches, {"https://a.test/": _result("HTTP_5XX", 503)}, deleted, sent)

    summary = public_watch.process_public_watches(object())

    assert summary["sent"] == 0
    assert sent == []
    assert deleted == []


def test_process_deletes_a_watch_whose_address_is_blocked(monkeypatch):
    deleted, sent = [], []
    watches = [_watch(1, "https://a.test/")]
    _setup(monkeypatch, watches, {"https://a.test/": None}, deleted, sent)

    summary = public_watch.process_public_watches(object())

    assert summary["blocked"] == 1
    assert deleted == [1]
    assert sent == []


def test_process_keeps_the_watch_when_the_telegram_message_fails(monkeypatch):
    deleted, sent = [], []
    watches = [_watch(1, "https://a.test/")]
    _setup(monkeypatch, watches, {"https://a.test/": _result("OK")}, deleted, sent)

    def boom(text, token, chat_id, timeout=10):
        raise alerts.TelegramError("down")

    monkeypatch.setattr(alerts, "send_telegram", boom)

    summary = public_watch.process_public_watches(object())

    assert summary["sent"] == 0
    assert deleted == []


def test_process_without_watches_does_nothing(monkeypatch):
    deleted, sent = [], []
    _setup(monkeypatch, [], {}, deleted, sent)

    summary = public_watch.process_public_watches(object())

    assert summary == {"expired": 0, "watched": 0, "sent": 0, "blocked": 0}
    assert sent == []
    assert deleted == []


def test_process_without_telegram_credentials_sends_nothing(monkeypatch):
    deleted, sent = [], []
    watches = [_watch(1, "https://a.test/")]
    _setup(monkeypatch, watches, {"https://a.test/": _result("OK")}, deleted, sent, credentials=None)

    summary = public_watch.process_public_watches(object())

    assert summary["watched"] == 1
    assert summary["sent"] == 0
    assert sent == []
    assert deleted == []


def test_process_skips_a_check_that_is_not_ready_within_the_deadline(monkeypatch):
    deleted, sent = [], []
    watches = [_watch(1, "https://a.test/")]
    _setup(monkeypatch, watches, {}, deleted, sent)

    def slow(url):
        time.sleep(0.5)
        return _result("OK")

    monkeypatch.setattr(public_watch, "_check_watch", slow)

    summary = public_watch.process_public_watches(object(), deadline=0.05)

    assert summary["sent"] == 0
    assert sent == []
    assert deleted == []


def test_process_reports_the_expired_watches(monkeypatch):
    deleted, sent = [], []
    _setup(monkeypatch, [], {}, deleted, sent, expired=3)

    summary = public_watch.process_public_watches(object())

    assert summary["expired"] == 3


def test_safe_wrapper_closes_the_connection_and_swallows_errors(monkeypatch):
    closed = []

    class _Conn:
        def close(self):
            closed.append(True)

    def boom(conn):
        raise db.DatabaseError("down")

    monkeypatch.setattr(public_check, "_open_connection", lambda: _Conn())
    monkeypatch.setattr(public_watch, "process_public_watches", boom)

    public_watch.process_public_watches_safely()

    assert closed == [True]


def test_safe_wrapper_hands_its_connection_to_the_processing(monkeypatch):
    seen = []

    class _Conn:
        def close(self):
            pass

    conn = _Conn()
    monkeypatch.setattr(public_check, "_open_connection", lambda: conn)
    monkeypatch.setattr(public_watch, "process_public_watches", lambda c: seen.append(c))

    public_watch.process_public_watches_safely()

    assert seen == [conn]


def test_run_checks_route_processes_the_watches(monkeypatch):
    calls = []
    secret = "cron-secret-0123456789abcdef"
    monkeypatch.setenv("CRON_SECRET", secret)
    monkeypatch.setattr(routes, "call_runner", lambda: {"sites_checked": 0})
    monkeypatch.setattr(routes, "process_public_watches_safely", lambda: calls.append(True))
    client = create_app({"TESTING": True}).test_client()

    response = client.post("/run-checks", headers={"X-Cron-Token": secret})

    assert response.status_code == 200
    assert calls == [True]


def test_check_watch_returns_none_for_a_blocked_address(monkeypatch):
    def blocked(url):
        raise SecurityError("blocked")

    monkeypatch.setattr(public_watch, "check_url_target", blocked)

    assert public_watch._check_watch("http://localhost/") is None


def test_check_watch_returns_the_check_result(monkeypatch):
    expected = _result("OK")
    monkeypatch.setattr(public_watch, "check_url_target", lambda url: None)
    monkeypatch.setattr(public_watch, "check_url", lambda url: expected)

    assert public_watch._check_watch("https://a.test/") is expected
