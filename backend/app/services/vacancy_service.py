"""Reading vacancies for a user: list with filters, sorting and cursor pagination, and details
(TD-13, TD-16, SOURCE-004, VACANCY-003/004)."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.pagination import InvalidCursorError, decode_cursor, encode_cursor
from app.models import Vacancy
from app.repositories.vacancy import Keyset, VacancyFilter, VacancyRepository
from app.schemas.vacancy import (
    DuplicateOut,
    FreshnessStatus,
    LocationOut,
    SalaryOut,
    SourceOut,
    VacancyDetailOut,
    VacancyListParams,
    VacancyPageOut,
    VacancySort,
    VacancySummaryOut,
)

# AMB-11: when a posting may no longer be accurate.
STALE_AFTER_PUBLICATION = timedelta(days=30)
STALE_AFTER_VERIFICATION = timedelta(days=7)


def freshness(vacancy: Vacancy, now: datetime) -> FreshnessStatus:
    if not vacancy.is_active:
        return FreshnessStatus.REMOVED
    verified_at = vacancy.last_verified_at or vacancy.collected_at
    if now - verified_at > STALE_AFTER_VERIFICATION:
        return FreshnessStatus.STALE
    if vacancy.published_at is not None and now - vacancy.published_at > STALE_AFTER_PUBLICATION:
        return FreshnessStatus.STALE
    return FreshnessStatus.FRESH


class VacancyService:
    def __init__(self, db: AsyncSession) -> None:
        self._vacancies = VacancyRepository(db)

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        params: VacancyListParams,
        *,
        search_job_id: uuid.UUID | None = None,
    ) -> VacancyPageOut:
        """`search_job_id` narrows the list to one run; the caller checks it is the user's."""
        now = datetime.now(UTC)
        page = await self._vacancies.list_visible(
            user_id,
            replace(_to_filter(params, now), search_job_id=search_job_id),
            sort=params.sort,
            after=_read_cursor(params.cursor, params.sort) if params.cursor else None,
            limit=params.limit,
        )
        duplicates = await self._duplicates_of(page.items)
        next_cursor = None
        if page.has_more and page.items:
            next_cursor = _write_cursor(params.sort, page.sort_values[-1], page.items[-1].id)
        return VacancyPageOut(
            items=[
                VacancySummaryOut(**_summary_fields(v, now, duplicates.get(v.id, [])))
                for v in page.items
            ],
            next_cursor=next_cursor,
        )

    async def get_for_user(self, user_id: uuid.UUID, vacancy_id: uuid.UUID) -> VacancyDetailOut:
        vacancy = await self._vacancies.get_visible(user_id, vacancy_id)
        if vacancy is None:
            # Missing and not visible to this user look the same (404, never 403).
            raise NotFoundError("Vacancy not found")
        duplicates = await self._duplicates_of([vacancy])
        return VacancyDetailOut(
            **_summary_fields(vacancy, datetime.now(UTC), duplicates.get(vacancy.id, [])),
            description=vacancy.description,
            requirements=vacancy.requirements,
            responsibilities=vacancy.responsibilities,
            skills=vacancy.skills,
            language=vacancy.language,
            salary_raw=vacancy.salary_raw,
            location_raw=vacancy.location_raw,
            removed_at=vacancy.removed_at,
        )

    async def _duplicates_of(self, vacancies: list[Vacancy]) -> dict[uuid.UUID, list[DuplicateOut]]:
        grouped = [v for v in vacancies if v.duplicate_group_id is not None]
        members = await self._vacancies.list_group_members(
            [v.duplicate_group_id for v in grouped if v.duplicate_group_id is not None],
            [v.id for v in vacancies],
        )
        by_group: dict[uuid.UUID, list[DuplicateOut]] = {}
        for member in members:
            if member.duplicate_group_id is not None:
                by_group.setdefault(member.duplicate_group_id, []).append(
                    DuplicateOut(source=_source(member), url=member.url)
                )
        return {
            v.id: by_group.get(v.duplicate_group_id, [])
            for v in grouped
            if v.duplicate_group_id is not None
        }


def _to_filter(params: VacancyListParams, now: datetime) -> VacancyFilter:
    return VacancyFilter(
        text=params.q,
        source_keys=params.source,
        remote_types=params.remote_type,
        employment_types=params.employment_type,
        experience_levels=params.experience_level,
        country=params.country,
        city=params.city,
        min_monthly_eur=Decimal(params.salary_min_eur)
        if params.salary_min_eur is not None
        else None,
        published_since=(
            now - timedelta(days=params.published_within_days)
            if params.published_within_days is not None
            else None
        ),
        include_inactive=params.include_inactive,
    )


def _write_cursor(sort: VacancySort, value: Any, last_id: uuid.UUID) -> str:
    serialized = (
        None if value is None else (value.isoformat() if sort is VacancySort.DATE else str(value))
    )
    return encode_cursor({"sort": sort.value, "value": serialized, "id": str(last_id)})


def _read_cursor(cursor: str, sort: VacancySort) -> Keyset:
    position = decode_cursor(cursor)
    if position.get("sort") != sort.value:
        raise InvalidCursorError()  # a cursor only continues the ordering it came from
    raw_value = position.get("value")
    try:
        last_id = uuid.UUID(str(position.get("id")))
        if raw_value is None:
            return Keyset(value=None, id=last_id)
        if sort is VacancySort.DATE:
            value: Any = datetime.fromisoformat(str(raw_value))
            if value.tzinfo is None:
                raise InvalidCursorError()
        else:
            value = Decimal(str(raw_value))
    except (ValueError, InvalidOperation) as exc:
        raise InvalidCursorError() from exc
    return Keyset(value=value, id=last_id)


def _source(vacancy: Vacancy) -> SourceOut:
    return SourceOut(
        key=vacancy.source.adapter_key,
        name=vacancy.source.name,
        attribution_text=vacancy.source.attribution_text,
    )


def _salary(vacancy: Vacancy) -> SalaryOut | None:
    if vacancy.salary_min is None and vacancy.salary_max is None:
        return None  # "Not specified", never an estimate (VACANCY-003)
    return SalaryOut(
        min=vacancy.salary_min,
        max=vacancy.salary_max,
        currency=vacancy.salary_currency,
        period=vacancy.salary_period,
    )


def _summary_fields(vacancy: Vacancy, now: datetime, also_on: list[DuplicateOut]) -> dict[str, Any]:
    return {
        "id": vacancy.id,
        "title": vacancy.title,
        "company": vacancy.company,
        "location": LocationOut(country=vacancy.country, region=vacancy.region, city=vacancy.city),
        "remote_type": vacancy.remote_type,
        "employment_type": vacancy.employment_type,
        "experience_level": vacancy.experience_level,
        "salary": _salary(vacancy),
        "description_snippet": vacancy.description_snippet,
        "url": vacancy.url,
        "source": _source(vacancy),
        "published_at": vacancy.published_at,
        "collected_at": vacancy.collected_at,
        "last_verified_at": vacancy.last_verified_at,
        "freshness": freshness(vacancy, now),
        "also_on": also_on,
    }
