"""Registry of job sources (TD-08).

Adding a source = an adapter module + one `SourceDefinition` below + fixtures and tests + a
section in docs/source-compliance.md. Nothing else changes: ingestion, deduplication, storage
and the vacancy API work on `VacancyIn` and don't know which source produced it.

A definition holds what is fixed by the source itself (name, host, terms, storage policy,
attribution). Whether a source may run is decided in the database (`sources.is_enabled` and
`compliance_reviewed_at`) by a person, never by code.
"""

from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.core.config import Settings
from app.core.enums import SourceType, StoragePolicy
from app.integrations.sources.adzuna.adapter import AdzunaAdapter
from app.integrations.sources.base import RateLimit, SourceAdapter, SourceNotConfiguredError
from app.integrations.sources.mock.adapter import MockSourceAdapter

USER_AGENT_PRODUCT = "AIJobAssistant/0.1"


@dataclass(frozen=True)
class SourceContext:
    """What an adapter may use: settings (credentials) and the shared HTTP client, which
    already carries the honest User-Agent and the timeout."""

    settings: Settings
    http: httpx.AsyncClient


@dataclass(frozen=True)
class SourceDefinition:
    key: str
    name: str
    base_url: str
    source_type: SourceType
    storage_policy: StoragePolicy
    rate_limit: RateLimit
    build: Callable[[SourceContext], SourceAdapter]
    terms_url: str | None = None
    attribution_text: str | None = None
    retention_days: int | None = None
    requires_network: bool = True


def user_agent(settings: Settings) -> str:
    """Honest identity with a contact, as SOURCE-003 requires. No browser imitation."""
    if settings.sources_contact:
        return f"{USER_AGENT_PRODUCT} (+{settings.sources_contact})"
    return USER_AGENT_PRODUCT


def _build_adzuna(context: SourceContext) -> SourceAdapter:
    settings = context.settings
    if not settings.sources_contact:
        raise SourceNotConfiguredError("SOURCES_CONTACT must be set before calling Adzuna")
    return AdzunaAdapter(
        app_id=settings.adzuna_app_id,
        app_key=settings.adzuna_app_key.get_secret_value() if settings.adzuna_app_key else None,
        http=context.http,
    )


SOURCES: dict[str, SourceDefinition] = {
    definition.key: definition
    for definition in (
        SourceDefinition(
            key=MockSourceAdapter().key,
            name="Mock job board",
            base_url="https://jobs.mock.test",
            source_type=SourceType.API,
            storage_policy=StoragePolicy.FULL,
            rate_limit=MockSourceAdapter().rate_limits(),
            build=lambda _context: MockSourceAdapter(),
            requires_network=False,
        ),
        SourceDefinition(
            key=AdzunaAdapter.key,
            name="Adzuna",
            base_url="https://api.adzuna.com",
            source_type=SourceType.API,
            # The API only returns a description snippet; we store no more than that.
            storage_policy=StoragePolicy.SNIPPET,
            rate_limit=AdzunaAdapter.RATE_LIMIT,
            build=_build_adzuna,
            terms_url="https://developer.adzuna.com/docs/terms_of_service",
            attribution_text="Jobs by Adzuna",
            retention_days=30,
        ),
    )
}


def get_source_definition(key: str) -> SourceDefinition | None:
    return SOURCES.get(key)
