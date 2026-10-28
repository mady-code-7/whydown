"""Tests for the public watch routes (Phase 8, part 4B).

No real database, no Telegram and no network access is used.
"""

from __future__ import annotations

import json
import re
import urllib.error

import pytest

from app import alerts, create_app, db, public_check, public_watch


@pytest.fixture
def closed():
    """A list that gets one entry for every connection that was closed."""
    return []


@pytest.fixture
def client(monkeypatch, closed):
    """A test client whose database connection is a fake."""

    class _Conn:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(public_check, "_open_connection", lambda: _Conn())
    return create_app({"TESTING": True}).test_client()


def _start_update(code, chat_id=111, update_id=1):
    return {
        "update_id": update_id,
        "message": {"text": "/start " + code, "chat": {"id": chat_id, "type": "private"}},
    }


class _Response:
    def __init__(self, body):
        self._body = body

    def read(self, size=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _telegram(monkeypatch, updates, sent):
    monkeypatch.setattr(alerts, "telegram_credentials", lambda: ("tok", "999"))
    monkeypatch.setattr(public_watch, "fetch_updates", lambda token: updates)

    def fake_send(text, token, chat_id, timeout=10):
        sent.append((text, token, chat_id))

    monkeypatch.setattr(alerts, "send_telegram", fake_send)


def test_new_code_is_url_safe_and_unique():
    first = public_watch.new_code()
    second = public_watch.new_code()
    assert re.fullmatch(r"[A-Za-z0-9_-]{8,64}", first)
    assert first != second


def test_start_link_has_the_bot_name_and_the_code():
    assert public_watch.start_link("abc12345") == "https://t.me/whydown_nm_bot?start=abc12345"


def test_find_chat_id_returns_the_chat_of_the_start_message():
    updates = [_start_update("code0001", 111)]
    assert public_watch.find_chat_id(updates, "code0001") == "111"


def test_find_chat_id_ignores_other_codes_and_other_texts():
    updates = [
        _start_update("other999", 1),
        {"update_id": 2, "message": {"text": "hello", "chat": {"id": 2}}},
    ]
    assert public_watch.find_chat_id(updates, "code0001") is None


def test_find_chat_id_uses_the_newest_matching_message():
    updates = [_start_update("code0001", 111, 1), _start_update("code0001", 222, 2)]
    assert public_watch.find_chat_id(updates, "code0001") == "222"


def test_find_chat_id_skips_malformed_updates():
    updates = [
        "text",
        5,
        {},
        {"message": "x"},
        {"message": {"text": "/start code0001"}},
        {"message": {"text": "/start code0001", "chat": {"id": True}}},
        _start_update("code0001", 333),
    ]
    assert public_watch.find_chat_id(updates, "code0001") == "333"


def test_fetch_updates_returns_the_result_list(monkeypatch):
    body = json.dumps({"ok": True, "result": [{"update_id": 1}]}).encode()
    seen = []

    def fake_urlopen(request, timeout=None):
        seen.append(request.full_url)
        return _Response(body)

    monkeypatch.setattr(public_watch.urllib.request, "urlopen", fake_urlopen)

    assert public_watch.fetch_updates("tok123") == [{"update_id": 1}]
    assert "/bottok123/getUpdates" in seen[0]
    assert "offset=-50" in seen[0]


def test_fetch_updates_not_ok_raises_telegram_error(monkeypatch):
    body = json.dumps({"ok": False}).encode()
    monkeypatch.setattr(
        public_watch.urllib.request, "urlopen", lambda request, timeout=None: _Response(body)
    )

    with pytest.raises(alerts.TelegramError):
        public_watch.fetch_updates("tok123")


def test_fetch_updates_http_error_message_has_no_token(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "nope", {}, None)

    monkeypatch.setattr(public_watch.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(alerts.TelegramError) as excinfo:
        public_watch.fetch_updates("SECRETTOKEN")
    assert "SECRETTOKEN" not in str(excinfo.value)
    assert "401" in str(excinfo.value)


def test_fetch_updates_network_error_message_has_no_token(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("cannot reach SECRETTOKEN")

    monkeypatch.setattr(public_watch.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(alerts.TelegramError) as excinfo:
        public_watch.fetch_updates("SECRETTOKEN")
    assert "SECRETTOKEN" not in str(excinfo.value)


def test_watch_route_creates_a_watch_and_returns_the_link(client, closed, monkeypatch):
    stored = []

    def fake_create(code, url, conn=None):
        stored.append((code, url))
        return 1

    monkeypatch.setattr(db, "create_public_watch", fake_create)

    response = client.post("/api/public/watch", json={"url": "https://Example.com"})

    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "ok"
    assert data["normalised_url"] == "https://example.com/"
    assert data["link"] == public_watch.start_link(data["code"])
    assert stored == [(data["code"], "https://example.com/")]
    assert closed == [True]
    assert response.headers["Access-Control-Allow-Origin"] == "*"


def test_watch_route_rejects_a_bad_address(client):
    response = client.post("/api/public/watch", json={"url": "ftp://example.com"})

    assert response.status_code == 400
    assert response.get_json()["status"] == "invalid_url"


def test_watch_route_without_a_url_is_a_400(client):
    response = client.post("/api/public/watch", json={})

    assert response.status_code == 400


def test_watch_route_database_failure_is_a_503(client, closed, monkeypatch):
    def boom(code, url, conn=None):
        raise db.DatabaseError("down")

    monkeypatch.setattr(db, "create_public_watch", boom)

    response = client.post("/api/public/watch", json={"url": "https://example.com"})

    assert response.status_code == 503
    assert closed == [True]


def test_watch_route_preflight_has_cors_headers(client):
    response = client.options("/api/public/watch")

    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == "*"


def test_confirm_route_saves_the_chat_and_sends_one_message(client, closed, monkeypatch):
    sent = []
    attached = []
    _telegram(monkeypatch, [_start_update("code0001", 111)], sent)

    def fake_attach(code, chat_id, conn=None):
        attached.append((code, chat_id))
        return {"id": 4, "normalised_url": "https://example.com/"}

    monkeypatch.setattr(db, "attach_public_watch_chat", fake_attach)

    response = client.post("/api/public/watch/confirm", json={"code": "code0001"})

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    assert attached == [("code0001", "111")]
    assert len(sent) == 1
    assert sent[0][2] == "111"
    assert "https://example.com/" in sent[0][0]
    assert closed == [True]


def test_confirm_route_waits_when_start_was_not_pressed(client, monkeypatch):
    sent = []
    _telegram(monkeypatch, [], sent)

    response = client.post("/api/public/watch/confirm", json={"code": "code0001"})

    assert response.status_code == 200
    assert response.get_json()["status"] == "waiting"
    assert sent == []


def test_confirm_route_unknown_or_expired_code_is_a_404(client, monkeypatch):
    sent = []
    _telegram(monkeypatch, [_start_update("code0001", 111)], sent)
    monkeypatch.setattr(db, "attach_public_watch_chat", lambda code, chat_id, conn=None: None)

    response = client.post("/api/public/watch/confirm", json={"code": "code0001"})

    assert response.status_code == 404
    assert response.get_json()["status"] == "expired"
    assert sent == []


def test_confirm_route_rejects_an_invalid_code(client):
    assert client.post("/api/public/watch/confirm", json={"code": "x"}).status_code == 400
    assert client.post("/api/public/watch/confirm", json={}).status_code == 400


def test_confirm_route_without_telegram_credentials_is_a_503(client, monkeypatch):
    monkeypatch.setattr(alerts, "telegram_credentials", lambda: None)

    response = client.post("/api/public/watch/confirm", json={"code": "code0001"})

    assert response.status_code == 503


def test_confirm_route_telegram_failure_is_a_503(client, monkeypatch):
    monkeypatch.setattr(alerts, "telegram_credentials", lambda: ("tok", "999"))

    def boom(token):
        raise alerts.TelegramError("down")

    monkeypatch.setattr(public_watch, "fetch_updates", boom)

    response = client.post("/api/public/watch/confirm", json={"code": "code0001"})

    assert response.status_code == 503


def test_confirm_route_database_failure_is_a_503(client, closed, monkeypatch):
    sent = []
    _telegram(monkeypatch, [_start_update("code0001", 111)], sent)

    def boom(code, chat_id, conn=None):
        raise db.DatabaseError("down")

    monkeypatch.setattr(db, "attach_public_watch_chat", boom)

    response = client.post("/api/public/watch/confirm", json={"code": "code0001"})

    assert response.status_code == 503
    assert closed == [True]
    assert sent == []


def test_confirm_route_preflight_has_cors_headers(client):
    response = client.options("/api/public/watch/confirm")

    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == "*"
