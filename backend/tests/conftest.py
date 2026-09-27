import pytest

from app.core.config import Environment, Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env=Environment.TEST,
        database_url="postgresql+asyncpg://app:app@localhost:5432/ai_job_assistant_test",
        redis_url="redis://localhost:6379/15",
        readiness_timeout_seconds=0.5,
    )
