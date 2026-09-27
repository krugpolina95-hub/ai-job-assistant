"""Import of vacancies from a source adapter (VACANCY-001..004, SOURCE-002/003, TD-16, TD-23).

    raw record → adapter.normalize → VacancyIn → adapter.validate → dedup → insert or update

Invalid records are skipped and counted; one bad record never fails the batch (SOURCE-002).
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import SourceNotEnabledError
from app.integrations.sources.base import RawVacancy, SourceAdapter
from app.models import Source, Vacancy
from app.repositories.source import SourceRepository
from app.repositories.vacancy import VacancyRepository
from app.schemas.vacancy import VacancyIn
from app.services import normalization

logger = structlog.stdlib.get_logger(__name__)

FUZZY_WINDOW = timedelta(days=30)
# TD-14 reference rates to EUR. Only EUR is known until the FX table lands (M5.11); other
# currencies get no monthly EUR value, so they are excluded from salary filters, not guessed.
EUR_RATES: dict[str, Decimal] = {"EUR": Decimal(1)}


class IngestOutcome(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    SKIPPED = "skipped"


@dataclass
class IngestResult:
    created: int = 0
    updated: int = 0
    grouped: int = 0
    skipped: int = 0
    vacancy_ids: list[uuid.UUID] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class VacancyIngestService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._sources = SourceRepository(db)
        self._vacancies = VacancyRepository(db)

    async def ingest(self, adapter: SourceAdapter, records: Iterable[RawVacancy]) -> IngestResult:
        source = await self._sources.get_by_key(adapter.key)
        if source is None or not source.is_enabled:
            # Compliance gate (TD-08): only registered, enabled (hence reviewed) sources run.
            raise SourceNotEnabledError(details={"source": adapter.key})

        result = IngestResult()
        now = datetime.now(UTC)
        for position, raw in enumerate(records):
            try:
                item = adapter.normalize(raw)
            except (ValueError, KeyError, TypeError) as exc:
                self._skip(result, position, f"normalize: {type(exc).__name__}")
                continue
            problems = adapter.validate(item)
            if problems:
                self._skip(result, position, "validate: " + "; ".join(problems))
                continue
            try:
                async with self._db.begin_nested():
                    vacancy, outcome, grouped = await self._upsert(source, item, now)
            except IntegrityError as exc:
                self._skip(result, position, f"constraint: {type(exc.orig).__name__}")
                continue
            result.vacancy_ids.append(vacancy.id)
            result.grouped += int(grouped)
            if outcome is IngestOutcome.CREATED:
                result.created += 1
            else:
                result.updated += 1

        await self._db.commit()
        logger.info(
            "vacancies_ingested",
            source=adapter.key,
            created=result.created,
            updated=result.updated,
            grouped=result.grouped,
            skipped=result.skipped,
        )
        return result

    async def _upsert(
        self, source: Source, item: VacancyIn, now: datetime
    ) -> tuple[Vacancy, IngestOutcome, bool]:
        values = _column_values(source, item, now)

        existing, related = await self._find_duplicates(source, item, values)
        if existing is not None:
            if existing.removed_at is not None and item.is_closed:
                values["removed_at"] = existing.removed_at  # keep when it was first reported
            for name, value in values.items():
                setattr(existing, name, value)
            existing.last_verified_at = now
            await self._vacancies.flush()
            return existing, IngestOutcome.UPDATED, False

        vacancy = self._vacancies.add(Vacancy(**values, collected_at=now, last_verified_at=now))
        await self._vacancies.flush()
        if related is not None:
            # Cross-source or near duplicate: group, never merge (TD-16, AMB-10).
            if related.duplicate_group_id is None:
                related.duplicate_group_id = related.id
            vacancy.duplicate_group_id = related.duplicate_group_id
            await self._vacancies.flush()
        return vacancy, IngestOutcome.CREATED, related is not None

    async def _find_duplicates(
        self, source: Source, item: VacancyIn, values: dict[str, Any]
    ) -> tuple[Vacancy | None, Vacancy | None]:
        """Returns (same record to update, other record to group with), per TD-16:
        1. same source + external id → same record
        2. canonical URL, 3. content hash → same record within the source, group across sources
        4. similar title + same company + same location within 30 days → group."""
        if item.external_id is not None:
            same = await self._vacancies.find_by_external_id(source.id, item.external_id)
            if same is not None:
                return same, None

        related: Vacancy | None = None
        for candidates in (
            await self._vacancies.find_by_canonical_url(values["canonical_url"]),
            await self._vacancies.find_by_content_hash(values["content_hash"]),
        ):
            for candidate in candidates:
                if candidate.source_id == source.id:
                    return candidate, None
                related = related or candidate
        if related is not None:
            return None, related

        if values["company_normalized"] is None:
            return None, None
        similar = await self._vacancies.find_similar(
            normalized_title=values["normalized_title"],
            company_normalized=values["company_normalized"],
            country=item.country,
            city=item.city,
            remote_type=item.remote_type,
            collected_since=datetime.now(UTC) - FUZZY_WINDOW,
        )
        return None, similar

    @staticmethod
    def _skip(result: IngestResult, position: int, reason: str) -> None:
        result.skipped += 1
        result.errors.append(f"record {position}: {reason}")


def _column_values(source: Source, item: VacancyIn, now: datetime) -> dict[str, Any]:
    text = normalization.html_to_text(item.description) if item.description else None
    description, description_snippet = normalization.apply_storage_policy(
        text or None, source.storage_policy
    )
    return {
        "source_id": source.id,
        "external_id": item.external_id,
        "url": item.url,
        "canonical_url": normalization.canonical_url(item.url),
        "title": normalization.collapse_whitespace(item.title),
        "normalized_title": normalization.normalize_title(item.title),
        "company": normalization.collapse_whitespace(item.company) if item.company else None,
        "company_normalized": normalization.normalize_company(item.company),
        "description": description,
        "description_snippet": description_snippet,
        "requirements": list(item.requirements),
        "responsibilities": list(item.responsibilities),
        "skills": list(item.skills),
        "language": item.language,
        "salary_min": item.salary_min,
        "salary_max": item.salary_max,
        "salary_currency": item.salary_currency,
        "salary_period": item.salary_period,
        "salary_raw": item.salary_raw,
        "salary_monthly_min_eur": normalization.to_monthly_eur(
            item.salary_min, item.salary_currency, item.salary_period, EUR_RATES
        ),
        "salary_monthly_max_eur": normalization.to_monthly_eur(
            item.salary_max, item.salary_currency, item.salary_period, EUR_RATES
        ),
        "country": item.country,
        "region": item.region,
        "city": item.city,
        "location_raw": item.location_raw,
        "remote_type": item.remote_type,
        "employment_type": item.employment_type,
        "experience_level": item.experience_level,
        "published_at": item.published_at,
        "content_hash": normalization.content_hash(
            title=item.title,
            company=item.company,
            country=item.country,
            city=item.city,
            remote_type=item.remote_type,
            description=text,
        ),
        "is_active": not item.is_closed,
        "removed_at": now if item.is_closed else None,
    }
