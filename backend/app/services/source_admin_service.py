"""Operator actions on sources (GAP-01, roadmap M8.5): register, enable, disable.

Enabling records that a person completed the compliance review (SRS §17): code never sets
`compliance_reviewed_at` on its own.
"""

from datetime import UTC, datetime
from http import HTTPStatus

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode, NotFoundError
from app.integrations.sources.registry import SOURCES, SourceDefinition
from app.models import Source
from app.repositories.source import SourceRepository

logger = structlog.stdlib.get_logger(__name__)


class ComplianceReviewRequiredError(AppError):
    code = ErrorCode.CONFLICT
    status_code = HTTPStatus.CONFLICT
    default_message = (
        "Source has no recorded compliance review. Complete docs/source-compliance.md, then "
        "enable it with the review confirmation."
    )


class SourceAdminService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._sources = SourceRepository(db)

    async def sync_definitions(self) -> list[str]:
        """Creates or refreshes a row per registered source from its definition. New rows
        start disabled; enablement and compliance fields are never touched here."""
        created: list[str] = []
        for definition in SOURCES.values():
            source = await self._sources.get_by_key(definition.key)
            if source is None:
                source = self._sources.add(Source(adapter_key=definition.key, is_enabled=False))
                created.append(definition.key)
            _apply_definition(source, definition)
        await self._db.commit()
        logger.info("sources_synced", created=created)
        return created

    async def list_sources(self) -> list[Source]:
        return await self._sources.list_all()

    async def enable(self, key: str, *, compliance_review_confirmed: bool) -> Source:
        source = await self._get(key)
        if source.compliance_reviewed_at is None:
            if not compliance_review_confirmed:
                raise ComplianceReviewRequiredError(details={"source": key})
            source.compliance_reviewed_at = datetime.now(UTC)
        source.is_enabled = True
        await self._db.commit()
        logger.info("source_enabled", source=key)
        return source

    async def disable(self, key: str) -> Source:
        source = await self._get(key)
        source.is_enabled = False
        await self._db.commit()
        logger.info("source_disabled", source=key)
        return source

    async def _get(self, key: str) -> Source:
        source = await self._sources.get_by_key(key)
        if source is None or key not in SOURCES:
            raise NotFoundError(f"Unknown source {key!r}; run `sources sync` first")
        return source


def _apply_definition(source: Source, definition: SourceDefinition) -> None:
    source.name = definition.name
    source.base_url = definition.base_url
    source.source_type = definition.source_type
    source.storage_policy = definition.storage_policy
    source.terms_url = definition.terms_url
    source.attribution_text = definition.attribution_text
    source.retention_days = definition.retention_days
    source.rate_limit = {
        "requests": definition.rate_limit.requests,
        "per_seconds": definition.rate_limit.per_seconds,
        "requests_per_day": definition.rate_limit.requests_per_day,
    }
