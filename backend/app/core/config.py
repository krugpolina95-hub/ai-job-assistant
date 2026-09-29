from datetime import timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from pydantic import (
    Field,
    HttpUrl,
    PostgresDsn,
    RedisDsn,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.logging import LogFormat


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    # env_ignore_empty: `VAR=` (as in .env.example and Compose's `${VAR:-}`) means "not set":
    # optional settings become None, required ones still fail fast as missing.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    app_env: Environment = Environment.LOCAL
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: LogFormat = "json"
    # Keyed hash for identifiers in logs (TD-21), so logs never contain raw user IDs.
    log_hash_secret: SecretStr = Field(min_length=32)

    database_url: PostgresDsn
    database_pool_size: int = Field(default=5, gt=0)
    redis_url: RedisDsn

    max_request_body_bytes: int = Field(default=64 * 1024, gt=0)
    readiness_timeout_seconds: float = Field(default=2.0, gt=0)

    # Sessions (TD-03). Values accept seconds or ISO 8601 durations (e.g. P7D).
    session_cookie_secure: bool = True
    session_idle_timeout: timedelta = timedelta(days=7)
    session_absolute_timeout: timedelta = timedelta(days=30)
    session_browser_timeout: timedelta = timedelta(hours=12)
    session_touch_interval: timedelta = timedelta(minutes=5)

    # Argon2id cost (TD-04). Tune so one hash takes ~100-250 ms on production hardware.
    password_hash_time_cost: int = Field(default=2, ge=1)
    password_hash_memory_kib: int = Field(default=19 * 1024, ge=8)
    password_hash_parallelism: int = Field(default=1, ge=1)

    # Brute-force protection (AUTH-006, architecture §4.4): this many failed logins in a row lock
    # the account for LOGIN_LOCKOUT_DURATION. Per-IP request limits are separate.
    login_lockout_threshold: int = Field(default=10, ge=3, le=100)
    login_lockout_duration: timedelta = timedelta(minutes=15)
    # Lifetime of the single-use link in a password reset email (AUTH-004).
    password_reset_token_ttl: timedelta = timedelta(minutes=30)

    # Client IP for rate limits and auth events (TD-05): how many proxies in front of the backend
    # append to X-Forwarded-For. Compose: 1 (Caddy; the Next.js rewrite keeps the header as is).
    # 0 = the TCP peer is the client (the backend is called directly).
    trusted_proxy_count: int = Field(default=0, ge=0, le=5)

    # Public origin of the web app, for links in emails, e.g. https://app.example.com.
    app_public_url: HttpUrl = HttpUrl("http://localhost")
    # Outgoing email over SMTP (TD-27). The defaults match Mailpit on the host.
    smtp_host: str = Field(default="localhost", min_length=1, max_length=255)
    smtp_port: int = Field(default=1025, ge=1, le=65535)
    smtp_security: Literal["none", "starttls", "tls"] = "none"
    smtp_username: str | None = Field(default=None, min_length=1, max_length=255)
    smtp_password: SecretStr | None = Field(default=None, min_length=1, max_length=255)
    smtp_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    email_from: str = Field(
        default="AI Job Assistant <no-reply@localhost>", min_length=3, max_length=200
    )

    # Outbound calls to job sources (SOURCE-003: honest identity, bounded time).
    # Contact shown in our User-Agent, e.g. an email or URL. Required before any real source runs.
    sources_contact: str | None = Field(default=None, min_length=3, max_length=200)
    source_http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    # Adzuna API credentials (source "adzuna", docs/source-compliance.md). Optional: without
    # them the adapter refuses to run.
    adzuna_app_id: str | None = Field(default=None, min_length=1, max_length=100)
    adzuna_app_key: SecretStr | None = Field(default=None, min_length=1, max_length=200)

    # Per-source API limits (TD-08, TD-18): longest wait for a rate-limit token before the source
    # fails with SOURCE_RATE_LIMITED, and automatic attempts per source in one search run.
    source_rate_limit_max_wait_seconds: float = Field(default=5.0, ge=0, le=60)
    source_run_max_attempts: int = Field(default=2, ge=1, le=5)
    source_retry_delay_seconds: float = Field(default=1.0, ge=0, le=30)
    # A claimed source run that hasn't reported back within this time may be claimed again.
    source_run_lease_seconds: int = Field(default=60, ge=5, le=3600)

    # Search runs (SEARCH-006, TD-12). User-level limits are separate from source limits above.
    # inline: runs in this process (dev, tests, fallback); n8n: signed webhook to WF-001 (TD-09).
    job_dispatcher: Literal["inline", "n8n"] = "inline"
    search_job_deadline_seconds: int = Field(default=120, ge=10, le=3600)
    search_max_active_jobs_per_user: int = Field(default=2, ge=1, le=20)
    search_max_runs_per_hour: int = Field(default=20, ge=1, le=1000)
    search_max_results_per_job: int = Field(default=200, ge=1, le=1000)

    # AI calls (TD-07). No provider is wired yet (product-owner decision); these bound any
    # provider: per-call timeout, retries on 429/5xx, circuit breaker, per-user quotas.
    ai_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    ai_max_retries: int = Field(default=1, ge=0, le=3)
    ai_retry_delay_seconds: float = Field(default=1.0, ge=0, le=10)
    ai_circuit_failure_threshold: int = Field(default=5, ge=1, le=100)
    ai_circuit_open_seconds: float = Field(default=60.0, gt=0, le=3600)
    ai_max_calls_per_minute: int = Field(default=10, ge=1, le=1000)
    ai_max_calls_per_day: int = Field(default=50, ge=1, le=10_000)

    # n8n integration (TD-06, TD-09, architecture §8.3). One secret per direction:
    # backend → n8n: N8N_WEBHOOK_SECRET signs the WF-001 trigger.
    # n8n → backend: INTERNAL_API_TOKEN (bearer) + INTERNAL_HMAC_SECRET sign /internal/v1 calls.
    # Without the last two the internal API refuses every request.
    n8n_webhook_url: HttpUrl | None = None
    n8n_webhook_secret: SecretStr | None = Field(default=None, min_length=32)
    n8n_webhook_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    internal_api_token: SecretStr | None = Field(default=None, min_length=32)
    internal_hmac_secret: SecretStr | None = Field(default=None, min_length=32)
    internal_signature_max_age_seconds: int = Field(default=300, ge=30, le=900)

    # Versions of the legal documents a user accepts at registration (AUTH-002, GAP-05).
    legal_terms_version: str = Field(min_length=1, max_length=64)
    legal_privacy_version: str = Field(min_length=1, max_length=64)

    @field_validator("database_url")
    @classmethod
    def require_asyncpg_driver(cls, value: PostgresDsn) -> PostgresDsn:
        if value.scheme != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg:// scheme")
        return value

    @model_validator(mode="after")
    def require_secure_cookies_when_deployed(self) -> Self:
        if self.app_env in (Environment.STAGING, Environment.PRODUCTION) and (
            not self.session_cookie_secure
        ):
            raise ValueError("SESSION_COOKIE_SECURE must be true in staging and production")
        return self

    @model_validator(mode="after")
    def require_real_email_settings_when_deployed(self) -> Self:
        if self.app_env not in (Environment.STAGING, Environment.PRODUCTION):
            return self
        if self.app_public_url.scheme != "https":
            raise ValueError("APP_PUBLIC_URL must be an https URL in staging and production")
        if self.smtp_security == "none":
            raise ValueError("SMTP_SECURITY must be starttls or tls in staging and production")
        if "localhost" in (self.smtp_host, self.email_from.rsplit("@", 1)[-1].rstrip(">")):
            raise ValueError("Set SMTP_HOST and EMAIL_FROM for staging and production")
        return self

    @model_validator(mode="after")
    def require_n8n_settings_for_the_n8n_dispatcher(self) -> Self:
        secrets = [self.n8n_webhook_secret, self.internal_api_token, self.internal_hmac_secret]
        if self.job_dispatcher == "n8n" and (
            self.n8n_webhook_url is None or any(secret is None for secret in secrets)
        ):
            raise ValueError(
                "JOB_DISPATCHER=n8n needs N8N_WEBHOOK_URL, N8N_WEBHOOK_SECRET, "
                "INTERNAL_API_TOKEN and INTERNAL_HMAC_SECRET"
            )
        values = [secret.get_secret_value() for secret in secrets if secret is not None]
        if len(values) != len(set(values)):
            raise ValueError(
                "N8N_WEBHOOK_SECRET, INTERNAL_API_TOKEN and INTERNAL_HMAC_SECRET must differ"
            )
        return self

    @property
    def internal_api_enabled(self) -> bool:
        return self.internal_api_token is not None and self.internal_hmac_secret is not None

    @property
    def public_origin(self) -> str:
        return str(self.app_public_url).rstrip("/")

    @property
    def expose_api_docs(self) -> bool:
        return self.app_env in (Environment.LOCAL, Environment.TEST)


@lru_cache
def get_settings() -> Settings:
    return Settings()
