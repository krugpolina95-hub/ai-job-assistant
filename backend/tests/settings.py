from pydantic import PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseTestSettings(BaseSettings):
    """Test-only configuration. Database tests are skipped when TEST_DATABASE_URL is unset."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    test_database_url: PostgresDsn | None = None
