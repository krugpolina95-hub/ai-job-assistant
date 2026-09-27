"""One run of one source: the pipeline n8n will trigger per source (TD-06, TD-08).

    external source → adapter.search → adapter.normalize → shared normalization
    → validation → deduplication → database        (the last four in VacancyIngestService)

A source failure is a result, not an exception: the caller (n8n, or the inline dispatcher)
records it per source and decides about retries, so one failing source never breaks a job.
"""

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import SearchJobSourceStatus
from app.core.errors import SourceNotEnabledError
from app.integrations.sources.base import SourceError, SourceTimeoutError
from app.integrations.sources.registry import SourceContext, get_source_definition, user_agent
from app.repositories.source import SourceRepository
from app.services.vacancy_ingest_service import VacancyIngestService

logger = structlog.stdlib.get_logger(__name__)


@dataclass
class SourceRunResult:
    source: str
    status: SearchJobSourceStatus
    fetched: int = 0
    created: int = 0
    updated: int = 0
    grouped: int = 0
    skipped: int = 0
    duration_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None
    vacancy_ids: list[uuid.UUID] = field(default_factory=list)


class SourceRunService:
    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    async def run(self, key: str, params: Mapping[str, Any]) -> SourceRunResult:
        definition = get_source_definition(key)
        source = await SourceRepository(self._db).get_by_key(key)
        # Compliance gate before any outbound request (TD-08, CLAUDE.md §7).
        if definition is None or source is None or not source.is_enabled:
            raise SourceNotEnabledError(details={"source": key})

        started = time.perf_counter()
        async with httpx.AsyncClient(
            timeout=self._settings.source_http_timeout_seconds,
            headers={"User-Agent": user_agent(self._settings)},
            follow_redirects=False,
        ) as http:
            try:
                adapter = definition.build(SourceContext(settings=self._settings, http=http))
                records = await adapter.search(params)
            except SourceError as exc:
                result = SourceRunResult(
                    source=key,
                    status=(
                        SearchJobSourceStatus.TIMED_OUT
                        if isinstance(exc, SourceTimeoutError)
                        else SearchJobSourceStatus.FAILED
                    ),
                    duration_ms=_elapsed_ms(started),
                    error_code=exc.code,
                    error_message=exc.message,
                )
                logger.warning(
                    "source_run_failed", source=key, error_code=exc.code, error=exc.message
                )
                return result

        ingested = await VacancyIngestService(self._db).ingest(adapter, records)
        result = SourceRunResult(
            source=key,
            status=SearchJobSourceStatus.SUCCEEDED,
            fetched=len(records),
            created=ingested.created,
            updated=ingested.updated,
            grouped=ingested.grouped,
            skipped=ingested.skipped,
            duration_ms=_elapsed_ms(started),
            vacancy_ids=ingested.vacancy_ids,
        )
        logger.info(
            "source_run_succeeded",
            source=key,
            fetched=result.fetched,
            created=result.created,
            updated=result.updated,
            skipped=result.skipped,
            duration_ms=result.duration_ms,
        )
        return result


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
