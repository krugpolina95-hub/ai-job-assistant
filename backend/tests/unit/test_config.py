import pytest
from pydantic import ValidationError

from app.core.config import Environment, Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("APP_ENV", "DATABASE_URL", "REDIS_URL"):
        monkeypatch.delenv(name, raising=False)


def test_reads_values_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/app")
    monkeypatch.setenv("REDIS_URL", "redis://cache:6379/0")

    settings = Settings(_env_file=None)

    assert settings.app_env is Environment.STAGING
    assert settings.database_url.hosts()[0]["host"] == "db"
    assert settings.expose_api_docs is False


@pytest.mark.parametrize("missing", ["DATABASE_URL", "REDIS_URL"])
def test_fails_fast_when_required_variable_is_missing(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/app")
    monkeypatch.setenv("REDIS_URL", "redis://cache:6379/0")
    monkeypatch.delenv(missing)

    with pytest.raises(ValidationError, match=missing.lower()):
        Settings(_env_file=None)


def test_rejects_sync_database_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db:5432/app")
    monkeypatch.setenv("REDIS_URL", "redis://cache:6379/0")

    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        Settings(_env_file=None)
