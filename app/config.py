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
    """Application configuration.

    ``secret_key`` signs the session cookie and ``admin_password`` opens the
    admin area. Both come from the environment and both are empty by
    default: a value that is not set must never be a secret of its own
    (spec section 12).
    """
    host: str = "0.0.0.0"
    port: int = 8080
    debug: bool = False
    secret_key: str = ""
    admin_password: str = ""
    max_sites: int = 20
    cron_secret: str = ""


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


# Minimum length of a usable CRON_SECRET. A shorter value is treated as
# "not set": a weak secret must never open the /run-checks endpoint.
CRON_SECRET_MIN_LENGTH = 16

# The default certificate-expiry warning tiers, in days. Every tier sends one
# warning when the remaining days drop to or below it (spec section 8.3.1).
# The tiers are kept in ascending order, so the most urgent one - the
# smallest number of days - is the first one.
DEFAULT_CERT_ALERT_DAYS = "14,7,3"

# The default tiers in ascending order: the most urgent tier comes first.
DEFAULT_CERT_ALERT_TIERS = (3, 7, 14)


def parse_cert_alert_days(raw: str | None) -> tuple[int, ...]:
    """Parse the certificate-expiry warning tiers from a string.

    The value is a comma separated list of day counts, for example
    ``"14,7,3"``. The tiers are returned in ascending order, without
    duplicates and without values that are not positive numbers. An empty or
    unusable value falls back to the default tiers, so the warnings keep
    working even with a broken environment variable.
    """
    if not raw:
        return DEFAULT_CERT_ALERT_TIERS

    days: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value > 0:
            days.add(value)

    if not days:
        return DEFAULT_CERT_ALERT_TIERS
    return tuple(sorted(days))


def load_cron_secret() -> str:
    """Read CRON_SECRET from the environment.

    Returns the secret, or an empty string when it is unset or shorter than
    ``CRON_SECRET_MIN_LENGTH``. The value is never logged, printed or put
    into an error message (spec section 12: secrets live in environment
    variables and never end up in a log).
    """
    secret = os.getenv("CRON_SECRET", "").strip()
    if len(secret) < CRON_SECRET_MIN_LENGTH:
        return ""
    return secret


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
    """Site threshold configuration.

    Three kinds of threshold are configured:

    * ``slow_threshold``          - milliseconds (spec section 8.3).
    * ``cert_expiry_warning_days`` - how many days remain before a certificate
      is flagged as expiring soon. Used by the checker's
      ``cert_expiring_soon`` flag (spec section 8.3.1).
    * ``cert_alert_thresholds``   - the certificate-expiry warning tiers. For
      every tier that the remaining days drop to or below, one warning is
      sent (spec section 8.3.1 and 8.4). A tier fires once per certificate,
      so a certificate that was renewed early (a different expiry date)
      starts the cycle again (test case T09). The default tiers are 14, 7
      and 3 days.
    """
    slow_threshold: int = 5000
    cert_expiry_warning_days: int = 7
    incident_threshold: int = 3
    # The warning tiers, kept ascending so that the lowest tier is found
    # first when several are crossed at the same time.
    cert_alert_thresholds: tuple[int, ...] = DEFAULT_CERT_ALERT_TIERS

    @property
    def cert_warning_days(self) -> int:
        """The window in which a certificate counts as expiring soon.

        The window is at least as wide as the widest warning tier, otherwise
        a tier could never be reached: the checker's ``cert_expiring_soon``
        flag has to be set before a warning can be sent (spec section 8.3.1).
        """
        widest = max(self.cert_alert_thresholds, default=0)
        return max(self.cert_expiry_warning_days, widest)


@dataclass(frozen=True)
class RunnerConfig:
    """Check-runner configuration (spec section 10).

    These bound how the checker engine runs:
    * ``max_workers``      - thread pool size for concurrent site checks.
    * ``site_timeout_seconds`` - hard per-site budget enforced from outside
      ``check_url``; a site that does not finish in time is recorded as a
      timeout instead of blocking the run.
    * ``run_deadline_seconds`` - total wall-clock budget for one run so the
      scheduler request always completes within its own timeout window.
    """
    max_workers: int = 8
    site_timeout_seconds: int = 10
    run_deadline_seconds: int = 25


@dataclass(frozen=True)
class Config:
    """Complete configuration for WhyDown."""
    database: DatabaseConfig
    app: AppConfig = AppConfig()
    alert: AlertConfig = AlertConfig(telegram_bot_token="", telegram_chat_id="")
    rate_limit: RateLimitConfig = RateLimitConfig()
    network_timeouts: NetworkTimeouts = NetworkTimeouts()
    thresholds: SiteThresholds = SiteThresholds()
    runner: RunnerConfig = RunnerConfig()

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

        # Runner config validation
        if self.runner.max_workers < 1:
            errors.append("MAX_WORKERS must be at least 1")
        if self.runner.site_timeout_seconds < 1:
            errors.append("SITE_TIMEOUT_SECONDS must be at least 1")
        if self.runner.run_deadline_seconds < 1:
            errors.append("RUN_DEADLINE_SECONDS must be at least 1")
        if self.runner.run_deadline_seconds <= self.runner.site_timeout_seconds:
            errors.append(
                "RUN_DEADLINE_SECONDS must be greater than SITE_TIMEOUT_SECONDS"
            )

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

    # Parse cron secret (min 16 chars; weaker values are treated as unset)
    cron_secret = load_cron_secret()

    # Parse app config
    app = AppConfig(
        host=os.getenv("FLASK_HOST", "0.0.0.0"),
        port=int(os.getenv("FLASK_PORT", "8080")),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
        secret_key=os.getenv("SECRET_KEY", ""),
        admin_password=os.getenv("ADMIN_PASSWORD", ""),
        max_sites=int(os.getenv("MAX_SITES", "20")),
        cron_secret=cron_secret,
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
        incident_threshold=int(os.getenv("INCIDENT_THRESHOLD", "3")),
        cert_alert_thresholds=parse_cert_alert_days(
            os.getenv("CERT_ALERT_DAYS", DEFAULT_CERT_ALERT_DAYS)
        ),
    )

    # Parse runner config
    runner = RunnerConfig(
        max_workers=int(os.getenv("MAX_WORKERS", "8")),
        site_timeout_seconds=int(os.getenv("SITE_TIMEOUT_SECONDS", "10")),
        run_deadline_seconds=int(os.getenv("RUN_DEADLINE_SECONDS", "25")),
    )

    return Config(
        database=database,
        app=app,
        alert=alert,
        rate_limit=rate_limit,
        network_timeouts=network_timeouts,
        thresholds=thresholds,
        runner=runner,
    )


# Load config instance
config = load_config()