"""Request signing between the backend and n8n (architecture §8.3) and its configuration."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.security import request_signature, signature_is_valid
from tests.conftest import build_test_settings

SECRET = "s" * 40
NOW = 1_800_000_000
REQUEST: dict[str, Any] = {
    "timestamp": str(NOW),
    "method": "POST",
    "path": "/internal/v1/jobs/abc/finalize",
    "body": b"",
}


def valid(**changes: Any) -> bool:
    signed = request_signature(SECRET, **REQUEST)
    checked = REQUEST | changes
    return signature_is_valid(
        changes.pop("secret", SECRET),
        changes.pop("signature", signed),
        timestamp=checked["timestamp"],
        method=checked["method"],
        path=checked["path"],
        body=checked["body"],
        now=changes.pop("now", NOW),
        max_age_seconds=300,
    )


def test_signature_format_is_stable() -> None:
    first = request_signature(SECRET, **REQUEST)

    assert first == request_signature(SECRET, **REQUEST)
    assert first.startswith("sha256=") and len(first) == len("sha256=") + 64


def test_the_signed_request_verifies() -> None:
    assert valid()
    assert valid(now=NOW + 300) and valid(now=NOW - 300)


@pytest.mark.parametrize(
    "change",
    [
        {"secret": "t" * 40},
        {"method": "GET"},
        {"path": "/internal/v1/jobs/other/finalize"},  # replay against another job
        {"path": "/internal/v1/maintenance/sweep-jobs"},  # or another endpoint
        {"body": b'{"status": "running"}'},
        {"timestamp": str(NOW + 1)},
        {"now": NOW + 301},  # too old
        {"now": NOW - 301},  # from the future
        {"signature": None},
        {"signature": "sha256=" + "0" * 64},
        {"timestamp": None},
        {"timestamp": "1.8e9"},
    ],
)
def test_any_change_breaks_the_signature(change: dict[str, Any]) -> None:
    assert not valid(**change)


N8N = {
    "job_dispatcher": "n8n",
    "n8n_webhook_url": "http://n8n:5678/webhook/wf-001-search",
    "n8n_webhook_secret": "w" * 32,
    "internal_api_token": "t" * 32,
    "internal_hmac_secret": "h" * 32,
}


def test_n8n_dispatcher_settings_are_accepted() -> None:
    settings = build_test_settings(**N8N)

    assert settings.job_dispatcher == "n8n"
    assert settings.internal_api_enabled


@pytest.mark.parametrize(
    "missing",
    ["n8n_webhook_url", "n8n_webhook_secret", "internal_api_token", "internal_hmac_secret"],
)
def test_n8n_dispatcher_needs_every_setting(missing: str) -> None:
    with pytest.raises(ValidationError, match="JOB_DISPATCHER=n8n"):
        build_test_settings(**(N8N | {missing: None}))


def test_each_direction_has_its_own_secret() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        build_test_settings(**(N8N | {"internal_hmac_secret": "w" * 32}))


def test_short_secrets_are_refused() -> None:
    with pytest.raises(ValidationError):
        build_test_settings(internal_hmac_secret="short")


def test_internal_api_is_off_without_its_secrets() -> None:
    assert not build_test_settings().internal_api_enabled
    assert not build_test_settings(internal_api_token="t" * 32).internal_api_enabled
