"""Minimal valid rows for database tests. Each builder adds to the session and flushes."""

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    AIInteractionStatus,
    AITask,
    HistoryEventType,
    MatchLevel,
    SearchChannel,
    SourceType,
    StoragePolicy,
)
from app.core.ids import uuid7
from app.models import (
    AIInteraction,
    SavedVacancy,
    Search,
    SearchHistoryEvent,
    SearchJob,
    SearchJobResult,
    SearchJobSource,
    Skill,
    Source,
    User,
    UserProfile,
    UserSession,
    UserSkill,
    Vacancy,
    VacancyMatch,
)


async def _persist[T](session: AsyncSession, obj: T) -> T:
    session.add(obj)
    await session.flush()
    return obj


async def make_user(session: AsyncSession, **overrides: Any) -> User:
    values: dict[str, Any] = {"email": f"user-{uuid7().hex}@example.com", "password_hash": "x"}
    return await _persist(session, User(**(values | overrides)))


async def make_source(session: AsyncSession, **overrides: Any) -> Source:
    values: dict[str, Any] = {
        "adapter_key": f"mock-{uuid7().hex}",
        "name": "Mock",
        "base_url": "https://jobs.example.com",
        "source_type": SourceType.API,
    }
    return await _persist(session, Source(**(values | overrides)))


async def make_vacancy(session: AsyncSession, source: Source, **overrides: Any) -> Vacancy:
    key = uuid7().hex
    values: dict[str, Any] = {
        "source_id": source.id,
        "external_id": key,
        "url": f"https://jobs.example.com/{key}",
        "canonical_url": f"https://jobs.example.com/{key}",
        "title": "Python Developer",
        "normalized_title": "python developer",
        "content_hash": hashlib.sha256(key.encode()).hexdigest(),
    }
    return await _persist(session, Vacancy(**(values | overrides)))


async def make_search(session: AsyncSession, user: User, **overrides: Any) -> Search:
    values: dict[str, Any] = {
        "user_id": user.id,
        "normalized_params": {"roles": ["python developer"]},
        "channel": SearchChannel.FORM,
    }
    return await _persist(session, Search(**(values | overrides)))


async def make_search_job(session: AsyncSession, search: Search, **overrides: Any) -> SearchJob:
    values: dict[str, Any] = {
        "search_id": search.id,
        "user_id": search.user_id,
        "context": {"params": search.normalized_params},
        "deadline_at": datetime.now(UTC) + timedelta(seconds=120),
    }
    return await _persist(session, SearchJob(**(values | overrides)))


async def make_enabled_source(
    session: AsyncSession, adapter_key: str | None = None, **overrides: Any
) -> Source:
    """A source that passed its compliance review, so ingestion may run for it."""
    values: dict[str, Any] = {
        "adapter_key": adapter_key or f"mock-{uuid7().hex}",
        "is_enabled": True,
        "compliance_reviewed_at": datetime.now(UTC),
        "storage_policy": StoragePolicy.FULL,
    }
    return await make_source(session, **(values | overrides))


async def show_to_user(session: AsyncSession, user_id: Any, vacancies: list[Vacancy]) -> SearchJob:
    """Make vacancies visible to a user the way search runs will: as results of their job."""
    search = await _persist(
        session,
        Search(user_id=user_id, normalized_params={"roles": ["any"]}, channel=SearchChannel.FORM),
    )
    job = await make_search_job(session, search)
    session.add_all(
        SearchJobResult(search_job_id=job.id, vacancy_id=vacancy.id, rank=rank)
        for rank, vacancy in enumerate(vacancies, start=1)
    )
    await session.flush()
    return job


async def make_user_graph(session: AsyncSession) -> dict[str, Any]:
    """A user with one row in every user-owned table, plus the shared rows they point to."""
    user = await make_user(session)
    source = await make_source(session)
    vacancy = await make_vacancy(session, source)
    skill = await _persist(session, Skill(name="Python", normalized_name=f"python-{uuid7().hex}"))
    search = await make_search(session, user)
    job = await make_search_job(session, search)

    session.add_all(
        [
            UserProfile(user_id=user.id, city="Vilnius"),
            UserSession(
                user_id=user.id,
                token_hash=hashlib.sha256(uuid7().bytes).hexdigest(),
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
            UserSkill(user_id=user.id, skill_id=skill.id),
            SearchJobSource(search_job_id=job.id, source_id=source.id),
            SearchJobResult(search_job_id=job.id, vacancy_id=vacancy.id, rank=1),
            VacancyMatch(
                user_id=user.id,
                search_job_id=job.id,
                vacancy_id=vacancy.id,
                match_level=MatchLevel.HIGH,
                algorithm_version=1,
            ),
            SavedVacancy(user_id=user.id, vacancy_id=vacancy.id),
            SearchHistoryEvent(
                user_id=user.id, search_id=search.id, event_type=HistoryEventType.SEARCH_RUN
            ),
            AIInteraction(
                user_id=user.id,
                search_id=search.id,
                task=AITask.PARSE_QUERY,
                provider="test",
                model="test-model",
                prompt_version="v1",
                status=AIInteractionStatus.SUCCEEDED,
            ),
        ]
    )
    await session.flush()
    return {
        "user": user,
        "source": source,
        "vacancy": vacancy,
        "skill": skill,
        "search": search,
        "job": job,
    }


def violated_constraint(error: IntegrityError) -> str | None:
    """Name of the Postgres constraint behind an IntegrityError (asyncpg driver)."""
    return getattr(error.orig.__cause__, "constraint_name", None)
