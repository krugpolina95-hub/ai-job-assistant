import pytest
from pydantic import ValidationError

from app.core.config import Environment, Settings

REQUIRED = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@db:5432/app",
    "REDIS_URL": "redis://cache:6379/0",
    "LOG_HASH_SECRET": "x" * 32,
    "LEGAL_TERMS_VERSION": "2026-09",
    "LEGAL_PRIVACY_VERSION": "2026-09",
}
# What staging and production need on top: real email delivery and an https app origin.
DEPLOYED = {
    "APP_PUBLIC_URL": "https://app.example.com",
    "SMTP_HOST": "smtp.example.com",
    "SMTP_SECURITY": "starttls",
    "EMAIL_FROM": "AI Job Assistant <no-reply@example.com>",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("APP_ENV", "SESSION_COOKIE_SECURE", *REQUIRED, *DEPLOYED):
        monkeypatch.delenv(name, raising=False)
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)


def deploy(monkeypatch: pytest.MonkeyPatch, app_env: str) -> None:
    monkeypatch.setenv("APP_ENV", app_env)
    for name, value in DEPLOYED.items():
        monkeypatch.setenv(name, value)


def test_reads_values_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    deploy(monkeypatch, "staging")

    settings = Settings(_env_file=None)

    assert settings.app_env is Environment.STAGING
    assert settings.database_url.hosts()[0]["host"] == "db"
    assert settings.expose_api_docs is False


@pytest.mark.parametrize("missing", list(REQUIRED))
def test_fails_fast_when_required_variable_is_missing(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delenv(missing)

    with pytest.raises(ValidationError, match=missing.lower()):
        Settings(_env_file=None)


def test_rejects_sync_database_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db:5432/app")

    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        Settings(_env_file=None)


def test_log_hash_secret_must_be_long_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_HASH_SECRET", "short")

    with pytest.raises(ValidationError, match="log_hash_secret"):
        Settings(_env_file=None)


def test_secret_is_not_exposed_in_repr() -> None:
    assert "x" * 32 not in repr(Settings(_env_file=None))


@pytest.mark.parametrize("app_env", ["staging", "production"])
def test_insecure_cookies_are_refused_when_deployed(
    monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    deploy(monkeypatch, app_env)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")

    with pytest.raises(ValidationError, match="SESSION_COOKIE_SECURE"):
        Settings(_env_file=None)


def test_insecure_cookies_are_allowed_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")

    assert Settings(_env_file=None).session_cookie_secure is False


def test_session_durations_accept_iso_8601(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_IDLE_TIMEOUT", "P3D")

    assert Settings(_env_file=None).session_idle_timeout.days == 3


def test_empty_optional_variables_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # Compose passes `${VAR:-}` and .env.example ships `VAR=`: both must mean "not set".
    for name in ("SOURCES_CONTACT", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"):
        monkeypatch.setenv(name, "")

    settings = Settings(_env_file=None)

    assert (settings.sources_contact, settings.adzuna_app_id, settings.adzuna_app_key) == (
        None,
        None,
        None,
    )


def test_empty_required_variable_still_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_HASH_SECRET", "")

    with pytest.raises(ValidationError, match="log_hash_secret"):
        Settings(_env_file=None)


@pytest.mark.parametrize("app_env", ["staging", "production"])
@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("APP_PUBLIC_URL", "http://app.example.com", "APP_PUBLIC_URL"),
        ("SMTP_SECURITY", "none", "SMTP_SECURITY"),
        ("SMTP_HOST", "localhost", "SMTP_HOST"),
        ("EMAIL_FROM", "AI Job Assistant <no-reply@localhost>", "EMAIL_FROM"),
    ],
)
def test_local_email_settings_are_refused_when_deployed(
    monkeypatch: pytest.MonkeyPatch, app_env: str, name: str, value: str, message: str
) -> None:
    deploy(monkeypatch, app_env)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None)


def test_local_defaults_need_no_email_configuration() -> None:
    settings = Settings(_env_file=None)

    assert (settings.smtp_host, settings.smtp_port, settings.smtp_security) == (
        "localhost",
        1025,
        "none",
    )
    assert settings.public_origin == "http://localhost"
    assert settings.trusted_proxy_count == 0


def test_lockout_and_reset_durations_accept_iso_8601(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOGIN_LOCKOUT_DURATION", "PT30M")
    monkeypatch.setenv("PASSWORD_RESET_TOKEN_TTL", "PT1H")

    settings = Settings(_env_file=None)

    assert settings.login_lockout_duration.total_seconds() == 1800
    assert settings.password_reset_token_ttl.total_seconds() == 3600
