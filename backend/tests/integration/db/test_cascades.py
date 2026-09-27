"""Deletion semantics: account deletion (AMB-14), history deletion (AMB-13), retention (TD-24)."""

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
    UserSkill,
    Vacancy,
    VacancyMatch,
)
from tests.integration.db.factories import (
    make_source,
    make_user_graph,
    make_vacancy,
    violated_constraint,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

USER_OWNED = (
    UserProfile,
    UserSkill,
    Search,
    SearchJob,
    SearchJobSource,
    SearchJobResult,
    VacancyMatch,
    SavedVacancy,
    SearchHistoryEvent,
    AIInteraction,
)


async def count(session: AsyncSession, model: type) -> int:
    return await session.scalar(select(func.count()).select_from(model)) or 0


async def test_deleting_a_user_removes_all_their_data_but_keeps_shared_data(
    db_session: AsyncSession,
) -> None:
    graph = await make_user_graph(db_session)
    assert all([await count(db_session, model) for model in USER_OWNED])

    await db_session.delete(graph["user"])
    await db_session.flush()
    db_session.expunge_all()

    for model in USER_OWNED:
        assert await count(db_session, model) == 0, model.__tablename__
    assert await db_session.get(User, graph["user"].id) is None
    assert await db_session.get(Vacancy, graph["vacancy"].id) is not None
    assert await db_session.get(Source, graph["source"].id) is not None
    assert await db_session.get(Skill, graph["skill"].id) is not None


async def test_deleting_a_search_removes_its_runs_and_keeps_ai_usage_records(
    db_session: AsyncSession,
) -> None:
    graph = await make_user_graph(db_session)

    await db_session.execute(delete(Search).where(Search.id == graph["search"].id))
    db_session.expunge_all()

    for model in (SearchJob, SearchJobSource, SearchJobResult, VacancyMatch, SearchHistoryEvent):
        assert await count(db_session, model) == 0, model.__tablename__
    interaction = await db_session.scalar(select(AIInteraction))
    assert interaction is not None
    assert interaction.search_id is None
    assert await count(db_session, SavedVacancy) == 1


async def test_saved_vacancy_cannot_be_deleted_by_retention(db_session: AsyncSession) -> None:
    graph = await make_user_graph(db_session)

    with pytest.raises(IntegrityError) as error:
        async with db_session.begin_nested():
            await db_session.execute(delete(Vacancy).where(Vacancy.id == graph["vacancy"].id))
    assert violated_constraint(error.value) == "fk_saved_vacancies_vacancy_id_vacancies"


async def test_unsaved_vacancy_deletion_removes_its_results_and_matches(
    db_session: AsyncSession,
) -> None:
    graph = await make_user_graph(db_session)
    await db_session.execute(delete(SavedVacancy))

    await db_session.execute(delete(Vacancy).where(Vacancy.id == graph["vacancy"].id))

    assert await count(db_session, SearchJobResult) == 0
    assert await count(db_session, VacancyMatch) == 0
    assert await count(db_session, SearchJob) == 1


async def test_source_with_vacancies_cannot_be_deleted(db_session: AsyncSession) -> None:
    source = await make_source(db_session)
    await make_vacancy(db_session, source)

    with pytest.raises(IntegrityError) as error:
        async with db_session.begin_nested():
            await db_session.execute(delete(Source).where(Source.id == source.id))
    assert violated_constraint(error.value) == "fk_vacancies_source_id_sources"
