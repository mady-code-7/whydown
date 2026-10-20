"""Environment configuration for WhyDown."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class DatabaseConfig:
    """Database connection configuration."""
    url: str
    pool_size: int = 5
    timeout: int = 30


@dataclass(frozen=True)
class AppConfig:
    """Application configuration."""
    host: str = "0.0.0.0"
    port: int = 8080
    debug: bool = False
    secret_key: str = "dev-secret-key"
    admin_password: str = "admin"
    max_sites: int = 20


@dataclass(frozen=True)
class AlertConfig:
    """Alert configuration."""
    telegram_bot_token: str
    telegram_chat_id: str
    email_enabled: bool = True
    email_host: str = "smtp.gmail.com"
    email_port: int = 587
    email_user: str = ""
    email_pass: str = ""


@dataclass(frozen=True)
class RateLimitConfig:
    """Rate limiting configuration."""
    max_sites: int = 20
    max_sites_per_hour: int = 60
    response_size_limit: int = 1048576
    redirects_followed: int = 5
    per_site_timeout: int = 10


@dataclass(frozen=True)
class NetworkTimeouts:
    """Network timeout configuration."""
    dns_timeout: int = 5
    connect_timeout: int = 8
    tls_timeout: int = 10
    read_timeout: int = 15
    total_timeout: int = 60


@dataclass(frozen=True)
class SiteThresholds:
    """Site threshold configuration."""
    slow_threshold: int = 5000
    cert_expiry_warning_days: int = 7
    incident_threshold: int = 2


@dataclass(frozen=True)
class Config:
    """Complete configuration for WhyDown."""
    database: DatabaseConfig
    app: AppConfig = AppConfig()
    alert: AlertConfig = AlertConfig(telegram_bot_token="", telegram_chat_id="")
    rate_limit: RateLimitConfig = RateLimitConfig()
    network_timeouts: NetworkTimeouts = NetworkTimeouts()
    thresholds: SiteThresholds = SiteThresholds()

    def validate(self) -> list[str]:
        """Validate configuration and return list of errors."""
        errors = []

        # Validate database URL
        try:
            urlparse(self.database.url)
        except ValueError as e:
            errors.append(f"Invalid DATABASE_URL: {e}")

        # Validate required alert config
        if not self.alert.telegram_bot_token:
            errors.append("TELEGRAM_BOT_TOKEN is required")
        if not self.alert.telegram_chat_id:
            errors.append("TELEGRAM_CHAT_ID is required")

        # Validate admin password for admin routes
        if not self.app.admin_password:
            errors.append("ADMIN_PASSWORD is required for admin functionality")

        # Validate numeric ranges
        if self.database.timeout <= 0:
            errors.append("DATABASE_TIMEOUT must be positive")

        return errors

    def is_valid(self) -> bool:
        """Check if configuration is valid."""
        return len(self.validate()) == 0


def load_config() -> Config:
    """Load configuration from environment variables."""
    # Parse DATABASE_URL
    database_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost/whydown")
    db_pool_size = int(os.getenv("DATABASE_POOL_SIZE", "5"))
    db_timeout = int(os.getenv("DATABASE_TIMEOUT", "30"))

    database = DatabaseConfig(
        url=database_url,
        pool_size=db_pool_size,
        timeout=db_timeout,
    )

    # Parse alert config
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat = os.getenv("TELEGRAM_CHAT_ID", "")

    alert = AlertConfig(
        telegram_bot_token=telegram_token,
        telegram_chat_id=telegram_chat,
        email_enabled=os.getenv("EMAIL_ENABLED", "true").lower() == "true",
        email_host=os.getenv("EMAIL_HOST", "smtp.gmail.com"),
        email_port=int(os.getenv("EMAIL_PORT", "587")),
        email_user=os.getenv("EMAIL_USER", ""),
        email_pass=os.getenv("EMAIL_PASS", ""),
    )

    # Parse app config
    app = AppConfig(
        host=os.getenv("FLASK_HOST", "0.0.0.0"),
        port=int(os.getenv("FLASK_PORT", "8080")),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
        secret_key=os.getenv("SECRET_KEY", "dev-secret-key"),
        admin_password=os.getenv("ADMIN_PASSWORD", "admin"),
        max_sites=int(os.getenv("MAX_SITES", "20")),
    )

    # Parse rate limit config
    rate_limit = RateLimitConfig(
        max_sites=int(os.getenv("MAX_SITES", "20")),
        max_sites_per_hour=int(os.getenv("MAX_SITES_PER_HOUR", "60")),
        response_size_limit=int(os.getenv("RESPONSE_SIZE_LIMIT", "1048576")),
        redirects_followed=int(os.getenv("REDIRECTS_FOLLOWED", "5")),
        per_site_timeout=int(os.getenv("PER_SITE_TIMEOUT", "10")),
    )

    # Parse network timeouts
    network_timeouts = NetworkTimeouts(
        dns_timeout=int(os.getenv("DNS_TIMEOUT", "5")),
        connect_timeout=int(os.getenv("CONNECT_TIMEOUT", "8")),
        tls_timeout=int(os.getenv("TLS_TIMEOUT", "10")),
        read_timeout=int(os.getenv("READ_TIMEOUT", "15")),
        total_timeout=int(os.getenv("TOTAL_TIMEOUT", "60")),
    )

    # Parse site thresholds
    thresholds = SiteThresholds(
        slow_threshold=int(os.getenv("SLOW_THRESHOLD_MS", "5000")),
        cert_expiry_warning_days=int(os.getenv("CERT_EXPIRY_WARNING_DAYS", "7")),
        incident_threshold=int(os.getenv("INCIDENT_THRESHOLD", "2")),
    )

    return Config(
        database=database,
        app=app,
        alert=alert,
        rate_limit=rate_limit,
        network_timeouts=network_timeouts,
        thresholds=thresholds,
    )


# Load config instance
config = load_config()