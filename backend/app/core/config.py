from datetime import timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, field_validator, model_validator
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

    # Outbound calls to job sources (SOURCE-003: honest identity, bounded time).
    # Contact shown in our User-Agent, e.g. an email or URL. Required before any real source runs.
    sources_contact: str | None = Field(default=None, min_length=3, max_length=200)
    source_http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    # Adzuna API credentials (source "adzuna", docs/source-compliance.md). Optional: without
    # them the adapter refuses to run.
    adzuna_app_id: str | None = Field(default=None, min_length=1, max_length=100)
    adzuna_app_key: SecretStr | None = Field(default=None, min_length=1, max_length=200)

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

    @property
    def expose_api_docs(self) -> bool:
        return self.app_env in (Environment.LOCAL, Environment.TEST)


@lru_cache
def get_settings() -> Settings:
    return Settings()
