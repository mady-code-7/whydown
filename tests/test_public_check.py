import json

import pytest

from app import db, public_check
from app.checker import CheckResult
from app.config import PublicConfig
from app.public_check import client_ip, normalise_url, run_public_check
from app.security import SecurityError, hash_visitor
from app.validation import ValidationError


def test_normalise_url_lowercases_the_host_and_keeps_the_path_case():
    assert normalise_url("https://EXAMPLE.com/Path") == "https://example.com/Path"


def test_normalise_url_adds_a_slash_for_an_empty_path():
    assert normalise_url("https://example.com") == "https://example.com/"


def test_normalise_url_drops_the_fragment_and_the_default_port():
    assert normalise_url("http://example.com:80/a#top") == "http://example.com/a"
    assert normalise_url("https://example.com:443/") == "https://example.com/"


def test_normalise_url_keeps_a_non_default_port_and_the_query():
    assert normalise_url("https://example.com:8443/a?x=1") == "https://example.com:8443/a?x=1"


def test_normalise_url_strips_spaces_around_the_url():
    assert normalise_url("  https://example.com/  ") == "https://example.com/"


def test_normalise_url_rejects_a_bad_scheme():
    with pytest.raises(ValidationError):
        normalise_url("ftp://example.com/")


def test_normalise_url_rejects_a_username_and_password():
    with pytest.raises(ValidationError):
        normalise_url("https://user:pass@example.com/")


def test_normalise_url_rejects_an_invalid_port():
    with pytest.raises(ValidationError):
        normalise_url("https://example.com:99999/")


IP = "203.0.113.5"
KEY = "test-hash-key-0123456789abcdef0123"


def _config(limit: int = 50) -> PublicConfig:
    return PublicConfig(ip_hash_key=KEY, rate_limit_per_hour=limit)


def _result(**changes) -> CheckResult:
    return CheckResult(**changes)


def _setup(monkeypatch, recent=None, count=0, blocked=False, result=None) -> dict:
    calls = {"recent": [], "count": [], "save": [], "check": [], "target": []}

    def fake_recent(*args, **kwargs):
        calls["recent"].append(args)
        return recent

    def fake_count(*args, **kwargs):
        calls["count"].append(args)
        return count

    def fake_save(*args, **kwargs):
        calls["save"].append(args)
        return 1

    def fake_check(url):
        calls["check"].append(url)
        return result if result is not None else _result()

    def fake_target(url):
        calls["target"].append(url)
        if blocked:
            raise SecurityError("blocked")

    monkeypatch.setattr(db, "get_recent_public_check", fake_recent)
    monkeypatch.setattr(db, "count_public_checks", fake_count)
    monkeypatch.setattr(db, "save_public_check", fake_save)
    monkeypatch.setattr(public_check, "check_url", fake_check)
    monkeypatch.setattr(public_check, "check_url_target", fake_target)
    return calls


def test_run_public_check_runs_a_new_check_and_saves_the_summary(monkeypatch):
    calls = _setup(monkeypatch)
    out = run_public_check("https://Example.com", IP, _config())
    assert out["status"] == "ok"
    assert out["normalised_url"] == "https://example.com/"
    assert calls["check"] == ["https://example.com/"]
    assert len(calls["save"]) == 1
    assert calls["save"][0][1] == "https://example.com/"
    assert json.loads(calls["save"][0][2]) == out["result"]
    assert out["result"]["result"] == "OK"


def test_run_public_check_returns_the_saved_result_for_a_repeat_url_without_a_new_check(monkeypatch):
    recent = {"id": 1, "normalised_url": "https://example.com/", "result_summary": json.dumps({"result": "OK"}), "checked_at": "2026-10-27"}
    calls = _setup(monkeypatch, recent=recent)
    out = run_public_check("https://example.com/", IP, _config())
    assert out["status"] == "cached"
    assert out["result"] == {"result": "OK"}
    assert out["checked_at"] == "2026-10-27"
    assert calls["check"] == []
    assert calls["save"] == []
    assert calls["target"] == []
    assert calls["count"] == []


def test_run_public_check_is_rate_limited_when_the_count_reaches_the_limit(monkeypatch):
    calls = _setup(monkeypatch, count=3)
    out = run_public_check("https://example.com/", IP, _config(3))
    assert out["status"] == "rate_limited"
    assert calls["check"] == []
    assert calls["save"] == []


def test_run_public_check_allows_a_check_when_the_count_is_below_the_limit(monkeypatch):
    _setup(monkeypatch, count=2)
    out = run_public_check("https://example.com/", IP, _config(3))
    assert out["status"] == "ok"


def test_run_public_check_blocks_an_unsafe_target_without_running_the_check(monkeypatch):
    calls = _setup(monkeypatch, blocked=True)
    out = run_public_check("https://example.com/", IP, _config())
    assert out["status"] == "blocked"
    assert calls["check"] == []
    assert calls["save"] == []


def test_run_public_check_returns_invalid_url_for_a_bad_address(monkeypatch):
    calls = _setup(monkeypatch)
    out = run_public_check("ftp://example.com/", IP, _config())
    assert out["status"] == "invalid_url"
    assert calls["recent"] == []
    assert calls["count"] == []
    assert calls["check"] == []
    assert calls["save"] == []


def test_run_public_check_passes_the_hashed_ip_and_never_the_raw_ip(monkeypatch):
    expected = hash_visitor(IP, KEY)
    calls = _setup(monkeypatch)
    run_public_check("https://example.com/", IP, _config())
    assert calls["recent"][0][0] == expected
    assert calls["count"][0][0] == expected
    assert calls["save"][0][0] == expected
    assert IP not in str(calls)


def test_run_public_check_summary_has_only_the_expected_keys(monkeypatch):
    _setup(monkeypatch)
    out = run_public_check("https://example.com/", IP, _config())
    assert set(out["result"]) == {"result", "failed_stage", "status_code", "dns_ms", "tcp_ms", "tls_ms", "ttfb_ms", "total_ms", "error_message", "cert_expired", "cert_expiring_soon", "cert_invalid"}


def test_run_public_check_cuts_a_long_error_message_to_300_characters(monkeypatch):
    _setup(monkeypatch, result=_result(error_message="x" * 500))
    out = run_public_check("https://example.com/", IP, _config())
    assert len(out["result"]["error_message"]) == 300


def test_client_ip_uses_the_first_forwarded_address():
    assert client_ip("203.0.113.5, 10.0.0.1", "198.51.100.7") == "203.0.113.5"


def test_client_ip_ignores_spaces_around_the_forwarded_address():
    assert client_ip("  203.0.113.5  ", None) == "203.0.113.5"


def test_client_ip_falls_back_to_the_remote_address_without_a_header():
    assert client_ip(None, "198.51.100.7") == "198.51.100.7"
    assert client_ip("", "198.51.100.7") == "198.51.100.7"


def test_client_ip_falls_back_when_the_first_forwarded_value_is_not_an_ip():
    assert client_ip("not-an-ip, 203.0.113.5", "198.51.100.7") == "198.51.100.7"


def test_client_ip_accepts_an_ipv6_forwarded_address():
    assert client_ip("2001:db8::1", None) == "2001:db8::1"


def test_client_ip_returns_unknown_when_nothing_is_available():
    assert client_ip(None, None) == "unknown"
    assert client_ip("", "  ") == "unknown"
