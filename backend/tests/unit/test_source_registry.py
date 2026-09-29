"""The registry is the only place a source is plugged in: every entry must be a valid port."""

import httpx
import pytest

from app.core.enums import StoragePolicy
from app.integrations.sources.base import SourceAdapter, SourceNotConfiguredError
from app.integrations.sources.registry import (
    SOURCES,
    USER_AGENT_PRODUCT,
    SourceContext,
    get_source_definition,
    user_agent,
)
from tests.conftest import build_test_settings

CONFIGURED = build_test_settings(
    sources_contact="ops@example.com", adzuna_app_id="id", adzuna_app_key="key"
)


@pytest.mark.parametrize("key", list(SOURCES))
async def test_every_registered_source_builds_a_conforming_adapter(key: str) -> None:
    definition = SOURCES[key]
    async with httpx.AsyncClient() as http:
        adapter = definition.build(SourceContext(settings=CONFIGURED, http=http))

    assert isinstance(adapter, SourceAdapter)
    assert adapter.key == definition.key == key
    assert adapter.rate_limits() == definition.rate_limit
    assert adapter.filter_support == definition.filter_support
    assert definition.base_url.startswith("https://")


def test_known_sources_and_lookup() -> None:
    assert set(SOURCES) == {"mock", "adzuna"}
    assert get_source_definition("adzuna") is SOURCES["adzuna"]
    assert get_source_definition("linkedin") is None


def test_adzuna_definition_follows_its_terms() -> None:
    adzuna = SOURCES["adzuna"]

    assert adzuna.storage_policy is StoragePolicy.SNIPPET  # the API only returns snippets
    assert adzuna.attribution_text == "Jobs by Adzuna"
    assert adzuna.terms_url == "https://developer.adzuna.com/docs/terms_of_service"


async def test_real_source_requires_a_contact_for_the_user_agent() -> None:
    settings = build_test_settings(adzuna_app_id="id", adzuna_app_key="key")
    async with httpx.AsyncClient() as http:
        with pytest.raises(SourceNotConfiguredError):
            SOURCES["adzuna"].build(SourceContext(settings=settings, http=http))


def test_user_agent_is_honest() -> None:
    assert user_agent(CONFIGURED) == f"{USER_AGENT_PRODUCT} (+ops@example.com)"
    assert "Mozilla" not in user_agent(CONFIGURED)
    assert user_agent(build_test_settings()) == USER_AGENT_PRODUCT
