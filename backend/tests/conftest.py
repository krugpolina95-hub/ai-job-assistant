from typing import Any

import pytest

from app.core.config import Environment, Settings

TEST_LOG_HASH_SECRET = "test-log-hash-secret-0123456789abcdef"


def build_test_settings(**overrides: Any) -> Settings:
    """Settings for tests, independent of any .env file. Hashing is made cheap on purpose."""
    values: dict[str, Any] = {
        "app_env": Environment.TEST,
        "database_url": "postgresql+asyncpg://app:app@localhost:5432/ai_job_assistant_test",
        "redis_url": "redis://localhost:6379/15",
        "readiness_timeout_seconds": 0.5,
        "log_hash_secret": TEST_LOG_HASH_SECRET,
        "legal_terms_version": "terms-test-1",
        "legal_privacy_version": "privacy-test-1",
        "password_hash_time_cost": 1,
        "password_hash_memory_kib": 1024,
        "password_hash_parallelism": 1,
    }
    return Settings(_env_file=None, **(values | overrides))


@pytest.fixture
def settings() -> Settings:
    return build_test_settings()
