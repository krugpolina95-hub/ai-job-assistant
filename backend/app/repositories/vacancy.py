"""Vacancy queries.

Vacancies are shared data, but a user may only read the ones *visible* to them: vacancies in
their own search-job results or in their saved list (architecture §8.2). Every read for the
API goes through `_visible_to(user_id)`; the dedup lookups used by ingestion are unscoped
because ingestion runs for the system, not for a user.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, case, func, literal, or_, select, union
from sqlalchemy.orm import joinedload

from app.core.enums import EmploymentType, ExperienceLevel, WorkFormat
from app.models import SavedVacancy, SearchJob, SearchJobResult, Source, Vacancy
from app.repositories.base import SharedRepository, escape_like
from app.schemas.vacancy import VacancySort

FUZZY_TITLE_SIMILARITY = 0.85


@dataclass(frozen=True)
class VacancyFilter:
    text: str | None = None
    source_keys: Sequence[str] = ()
    remote_types: Sequence[WorkFormat] = ()
    employment_types: Sequence[EmploymentType] = ()
    experience_levels: Sequence[ExperienceLevel] = ()
    country: str | None = None
    city: str | None = None
    min_monthly_eur: Decimal | None = None
    published_since: datetime | None = None
    include_inactive: bool = False
    search_job_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Keyset:
    """Position after the last row of the previous page."""

    value: Any
    id: uuid.UUID


@dataclass(frozen=True)
class VacancyPage:
    items: list[Vacancy]
    sort_values: list[Any] = field(default_factory=list)
    has_more: bool = False


def _sort_expression(sort: VacancySort) -> ColumnElement[Any]:
    if sort is VacancySort.SALARY:
        return func.coalesce(Vacancy.salary_monthly_max_eur, Vacancy.salary_monthly_min_eur)
    return Vacancy.published_at  # type: ignore[return-value]


def _completeness() -> ColumnElement[int]:
    """How much a record says; the most complete one represents a duplicate group (TD-16)."""
    columns = (
        Vacancy.company,
        Vacancy.salary_min,
        Vacancy.published_at,
        Vacancy.description_snippet,
        Vacancy.city,
        Vacancy.employment_type,
        Vacancy.experience_level,
    )
    total: ColumnElement[int] = literal(0)
    for column in columns:
        total = total + case((column.is_not(None), 1), else_=0)
    return total


class VacancyRepository(SharedRepository[Vacancy]):
    model = Vacancy

    # --- dedup lookups for ingestion (TD-16) ----------------------------------------------

    async def find_by_external_id(self, source_id: uuid.UUID, external_id: str) -> Vacancy | None:
        vacancy: Vacancy | None = await self.session.scalar(
            select(Vacancy).where(
                Vacancy.source_id == source_id, Vacancy.external_id == external_id
            )
        )
        return vacancy

    async def find_by_canonical_url(self, url: str) -> list[Vacancy]:
        result = await self.session.scalars(
            select(Vacancy).where(Vacancy.canonical_url == url).order_by(Vacancy.collected_at)
        )
        return list(result)

    async def find_by_content_hash(self, content_hash: str) -> list[Vacancy]:
        result = await self.session.scalars(
            select(Vacancy)
            .where(Vacancy.content_hash == content_hash)
            .order_by(Vacancy.collected_at)
        )
        return list(result)

    async def find_similar(
        self,
        *,
        normalized_title: str,
        company_normalized: str,
        country: str | None,
        city: str | None,
        remote_type: WorkFormat | None,
        collected_since: datetime,
    ) -> Vacancy | None:
        """Fuzzy duplicate (TD-16 step 4): similar title, same company and location, recent.
        `%` lets Postgres use the trigram index; the explicit threshold is the real rule."""
        similarity = func.similarity(Vacancy.normalized_title, normalized_title)
        statement = (
            select(Vacancy)
            .where(
                Vacancy.normalized_title.op("%")(normalized_title),
                similarity >= FUZZY_TITLE_SIMILARITY,
                Vacancy.company_normalized == company_normalized,
                Vacancy.country.is_not_distinct_from(country),
                func.lower(Vacancy.city).is_not_distinct_from(city.lower() if city else None),
                Vacancy.remote_type.is_not_distinct_from(remote_type),
                Vacancy.collected_at >= collected_since,
            )
            .order_by(similarity.desc(), Vacancy.collected_at)
            .limit(1)
        )
        vacancy: Vacancy | None = await self.session.scalar(statement)
        return vacancy

    # --- reads for a user -----------------------------------------------------------------

    async def get_visible(self, user_id: uuid.UUID, vacancy_id: uuid.UUID) -> Vacancy | None:
        statement = (
            select(Vacancy)
            .options(joinedload(Vacancy.source))
            .where(Vacancy.id == vacancy_id, Vacancy.id.in_(_visible_to(user_id)))
        )
        vacancy: Vacancy | None = await self.session.scalar(statement)
        return vacancy

    async def list_visible(
        self,
        user_id: uuid.UUID,
        filters: VacancyFilter,
        *,
        sort: VacancySort,
        after: Keyset | None,
        limit: int,
    ) -> VacancyPage:
        """One row per duplicate group (its most complete record), filtered, sorted with
        unknown values last, and paginated by keyset (value, id), both descending."""
        sort_value = _sort_expression(sort)
        group = func.coalesce(Vacancy.duplicate_group_id, Vacancy.id)
        candidates = (
            _apply_filters(
                select(
                    Vacancy.id.label("id"),
                    sort_value.label("sort_value"),
                    func.row_number()
                    .over(
                        partition_by=group,
                        order_by=(_completeness().desc(), Vacancy.collected_at.desc(), Vacancy.id),
                    )
                    .label("group_rank"),
                ).join(Source, Source.id == Vacancy.source_id),
                filters,
            )
            .where(Vacancy.id.in_(_visible_to(user_id)))
            .subquery()
        )
        statement = (
            select(Vacancy, candidates.c.sort_value)
            .join(candidates, candidates.c.id == Vacancy.id)
            .options(joinedload(Vacancy.source))
            .where(candidates.c.group_rank == 1)
            .order_by(candidates.c.sort_value.desc().nulls_last(), Vacancy.id.desc())
            .limit(limit + 1)
        )
        if after is not None:
            statement = statement.where(_after(candidates.c.sort_value, after))

        rows = (await self.session.execute(statement)).all()
        page = rows[:limit]
        return VacancyPage(
            items=[row[0] for row in page],
            sort_values=[row[1] for row in page],
            has_more=len(rows) > limit,
        )

    async def list_group_members(
        self, group_ids: Sequence[uuid.UUID], exclude_ids: Sequence[uuid.UUID]
    ) -> list[Vacancy]:
        if not group_ids:
            return []
        result = await self.session.scalars(
            select(Vacancy)
            .options(joinedload(Vacancy.source))
            .where(Vacancy.duplicate_group_id.in_(group_ids), Vacancy.id.not_in(exclude_ids))
            .order_by(Vacancy.collected_at)
        )
        return list(result)


def _visible_to(user_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
    in_results = (
        select(SearchJobResult.vacancy_id)
        .join(SearchJob, SearchJob.id == SearchJobResult.search_job_id)
        .where(SearchJob.user_id == user_id)
    )
    saved = select(SavedVacancy.vacancy_id).where(SavedVacancy.user_id == user_id)
    return select(union(in_results, saved).subquery().c.vacancy_id)


def _apply_filters[T: tuple[Any, ...]](statement: Select[T], filters: VacancyFilter) -> Select[T]:
    conditions: list[ColumnElement[bool]] = []
    if filters.search_job_id is not None:
        conditions.append(
            Vacancy.id.in_(
                select(SearchJobResult.vacancy_id).where(
                    SearchJobResult.search_job_id == filters.search_job_id
                )
            )
        )
    if not filters.include_inactive:
        conditions.append(Vacancy.is_active.is_(True))
    if filters.text:
        pattern = f"%{escape_like(filters.text)}%"
        conditions.append(
            or_(
                Vacancy.title.ilike(pattern, escape="\\"),
                Vacancy.company.ilike(pattern, escape="\\"),
            )
        )
    if filters.source_keys:
        conditions.append(Source.adapter_key.in_(filters.source_keys))
    if filters.remote_types:
        conditions.append(Vacancy.remote_type.in_(filters.remote_types))
    if filters.employment_types:
        conditions.append(Vacancy.employment_type.in_(filters.employment_types))
    if filters.experience_levels:
        conditions.append(Vacancy.experience_level.in_(filters.experience_levels))
    if filters.country:
        conditions.append(Vacancy.country == filters.country)
    if filters.city:
        conditions.append(func.lower(Vacancy.city) == filters.city.lower())
    if filters.min_monthly_eur is not None:
        # A range qualifies when its top reaches the requested minimum.
        conditions.append(
            func.coalesce(Vacancy.salary_monthly_max_eur, Vacancy.salary_monthly_min_eur)
            >= filters.min_monthly_eur
        )
    if filters.published_since is not None:
        conditions.append(Vacancy.published_at >= filters.published_since)
    return statement.where(and_(*conditions)) if conditions else statement


def _after(sort_value: ColumnElement[Any], position: Keyset) -> ColumnElement[bool]:
    """Rows strictly after `position` in `ORDER BY sort_value DESC NULLS LAST, id DESC`."""
    if position.value is None:
        return and_(sort_value.is_(None), Vacancy.id < position.id)
    return or_(
        sort_value < position.value,
        and_(sort_value == position.value, Vacancy.id < position.id),
        sort_value.is_(None),
    )
