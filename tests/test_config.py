"""Tests for environment-based configuration."""

from app.config import load_config, parse_cert_alert_days


def test_config_loads_from_env_vars(monkeypatch):
    """Config should read values from environment variables."""
    monkeypatch.setenv("FLASK_HOST", "127.0.0.1")
    monkeypatch.setenv("FLASK_PORT", "9999")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/testdb")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
    monkeypatch.setenv("MAX_SITES", "50")

    cfg = load_config()

    assert cfg.app.host == "127.0.0.1"
    assert cfg.app.port == 9999
    assert cfg.database.url == "postgresql://user:pass@localhost/testdb"
    assert cfg.alert.telegram_bot_token == "test-token"
    assert cfg.alert.telegram_chat_id == "test-chat"
    assert cfg.rate_limit.max_sites == 50


def test_config_validate_rejects_missing_required_values(monkeypatch):
    """validate() should reject missing TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")

    cfg = load_config()

    errors = cfg.validate()
    assert "TELEGRAM_BOT_TOKEN is required" in errors
    assert "TELEGRAM_CHAT_ID is required" in errors


def test_config_validate_accepts_valid_config(monkeypatch):
    """validate() should return no errors for a complete config."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/testdb")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
    monkeypatch.setenv("ADMIN_PASSWORD", "secure-pass")
    monkeypatch.setenv("DATABASE_TIMEOUT", "10")

    cfg = load_config()

    assert cfg.is_valid() is True
    assert cfg.validate() == []


def test_incident_threshold_defaults_to_three(monkeypatch):
    """Three consecutive failures open an incident (spec section 8.4)."""
    monkeypatch.delenv("INCIDENT_THRESHOLD", raising=False)

    cfg = load_config()

    assert cfg.thresholds.incident_threshold == 3


def test_incident_threshold_is_read_from_the_environment(monkeypatch):
    """The threshold can be changed without touching the code."""
    monkeypatch.setenv("INCIDENT_THRESHOLD", "5")

    cfg = load_config()

    assert cfg.thresholds.incident_threshold == 5


def test_cert_alert_days_default_to_14_7_and_3(monkeypatch):
    """Without a configuration the warning uses three tiers (spec section 8.3.1)."""
    monkeypatch.delenv("CERT_ALERT_DAYS", raising=False)

    cfg = load_config()

    assert cfg.thresholds.cert_alert_thresholds == (3, 7, 14)


def test_cert_alert_days_are_read_from_the_environment(monkeypatch):
    """The tiers can be changed without touching the code."""
    monkeypatch.setenv("CERT_ALERT_DAYS", "30,10")

    cfg = load_config()

    assert cfg.thresholds.cert_alert_thresholds == (10, 30)


def test_a_broken_cert_alert_days_falls_back_to_the_default(monkeypatch):
    """An unusable value does not switch the warnings off."""
    monkeypatch.setenv("CERT_ALERT_DAYS", "not a number")

    cfg = load_config()

    assert cfg.thresholds.cert_alert_thresholds == (3, 7, 14)


def test_the_warning_window_covers_the_widest_tier(monkeypatch):
    """The flag of the checker must be set before a tier can fire."""
    monkeypatch.setenv("CERT_EXPIRY_WARNING_DAYS", "7")
    monkeypatch.setenv("CERT_ALERT_DAYS", "14,7,3")

    cfg = load_config()

    assert cfg.thresholds.cert_warning_days == 14


def test_a_wider_warning_window_is_kept(monkeypatch):
    """A window that is wider than the tiers is not narrowed down."""
    monkeypatch.setenv("CERT_EXPIRY_WARNING_DAYS", "30")
    monkeypatch.setenv("CERT_ALERT_DAYS", "14,7,3")

    cfg = load_config()

    assert cfg.thresholds.cert_warning_days == 30


def test_parse_cert_alert_days_sorts_and_filters():
    """The parser ignores empty, zero and unusable values."""
    assert parse_cert_alert_days(None) == (3, 7, 14)
    assert parse_cert_alert_days("") == (3, 7, 14)
    assert parse_cert_alert_days("14,7,3") == (3, 7, 14)
    assert parse_cert_alert_days(" 14 , 7 , 0 , -3 ") == (7, 14)
    assert parse_cert_alert_days("0,0") == (3, 7, 14)