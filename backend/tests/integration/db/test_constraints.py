from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import SearchJobStatus, StoragePolicy
from app.models import SavedVacancy, SearchJob, User, UserProfile
from tests.integration.db.factories import (
    make_search,
    make_search_job,
    make_source,
    make_user,
    make_vacancy,
    violated_constraint,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def expect_violation(
    session: AsyncSession, constraint: str, statement: str, row_id: UUID
) -> None:
    """Runs raw SQL (bypassing ORM/enum coercion) so the database constraint itself is tested."""
    with pytest.raises(IntegrityError) as error:
        async with session.begin_nested():
            await session.execute(text(statement), {"id": row_id})
    assert violated_constraint(error.value) == constraint


async def test_defaults_ids_and_timestamps(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    job = await make_search_job(db_session, await make_search(db_session, user))
    await db_session.refresh(user)
    await db_session.refresh(job)

    assert user.id.version == 7
    assert user.created_at.tzinfo is not None
    assert user.is_verified is False
    assert user.preferred_locale == "en"
    assert job.status is SearchJobStatus.PENDING
    assert job.progress == {}


async def test_updated_at_changes_on_orm_update(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    past = datetime(2020, 1, 1, tzinfo=UTC)
    await db_session.execute(update(User).where(User.id == user.id).values(updated_at=past))
    await db_session.refresh(user)

    user.timezone = "Europe/Vilnius"
    await db_session.flush()
    await db_session.refresh(user)

    assert user.updated_at > past


async def test_email_is_unique_case_insensitively(db_session: AsyncSession) -> None:
    await make_user(db_session, email="Ada@Example.com")

    with pytest.raises(IntegrityError) as error:
        async with db_session.begin_nested():
            await make_user(db_session, email="ada@example.COM")
    assert violated_constraint(error.value) == "uq_users_email"

    found = await db_session.scalar(select(User).where(User.email == "ADA@example.com"))
    assert found is not None


async def test_enum_columns_round_trip_and_reject_unknown_values(db_session: AsyncSession) -> None:
    source = await make_source(db_session, storage_policy=StoragePolicy.SNIPPET)
    await db_session.refresh(source)
    assert source.storage_policy is StoragePolicy.SNIPPET

    await expect_violation(
        db_session,
        "ck_sources_storage_policy",
        "UPDATE sources SET storage_policy = 'everything' WHERE id = :id",
        source.id,
    )


async def test_source_cannot_be_enabled_before_compliance_review(db_session: AsyncSession) -> None:
    source = await make_source(db_session)

    await expect_violation(
        db_session,
        "ck_sources_enabled_requires_review",
        "UPDATE sources SET is_enabled = true WHERE id = :id",
        source.id,
    )

    source.compliance_reviewed_at = datetime.now(UTC)
    source.is_enabled = True
    await db_session.flush()


async def test_salary_range_and_currency_are_validated(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    db_session.add(UserProfile(user_id=user.id))
    await db_session.flush()

    await expect_violation(
        db_session,
        "ck_user_profiles_salary_min_le_salary_max",
        "UPDATE user_profiles SET salary_min = 5000, salary_max = 3000 WHERE user_id = :id",
        user.id,
    )
    await expect_violation(
        db_session,
        "ck_user_profiles_salary_currency_iso4217",
        "UPDATE user_profiles SET salary_currency = 'eur' WHERE user_id = :id",
        user.id,
    )

    vacancy = await make_vacancy(db_session, await make_source(db_session))
    await expect_violation(
        db_session,
        "ck_vacancies_salary_min_le_salary_max",
        "UPDATE vacancies SET salary_min = 10, salary_max = 5 WHERE id = :id",
        vacancy.id,
    )


async def test_vacancy_external_id_is_unique_per_source_when_present(
    db_session: AsyncSession,
) -> None:
    source = await make_source(db_session)
    await make_vacancy(db_session, source, external_id="42")

    with pytest.raises(IntegrityError) as error:
        async with db_session.begin_nested():
            await make_vacancy(db_session, source, external_id="42")
    assert violated_constraint(error.value) == "uq_vacancies_source_id_external_id"

    await make_vacancy(db_session, source, external_id=None)
    await make_vacancy(db_session, source, external_id=None)
    await make_vacancy(db_session, await make_source(db_session), external_id="42")


async def test_vacancy_rejects_invented_or_inconsistent_data(db_session: AsyncSession) -> None:
    vacancy = await make_vacancy(db_session, await make_source(db_session))

    await expect_violation(
        db_session,
        "ck_vacancies_url_http",
        "UPDATE vacancies SET url = 'javascript:alert(1)' WHERE id = :id",
        vacancy.id,
    )
    await expect_violation(
        db_session,
        "ck_vacancies_active_matches_removed_at",
        "UPDATE vacancies SET is_active = false WHERE id = :id",
        vacancy.id,
    )
    await expect_violation(
        db_session,
        "ck_vacancies_description_snippet_length",
        "UPDATE vacancies SET description_snippet = repeat('a', 501) WHERE id = :id",
        vacancy.id,
    )


async def test_idempotency_key_is_unique_per_user(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    search = await make_search(db_session, user)
    done = {"status": SearchJobStatus.COMPLETED}  # finished runs: only the key can collide
    await make_search_job(db_session, search, idempotency_key="click-1", **done)

    with pytest.raises(IntegrityError) as error:
        async with db_session.begin_nested():
            await make_search_job(db_session, search, idempotency_key="click-1", **done)
    assert violated_constraint(error.value) == "uq_search_jobs_user_id_idempotency_key"

    other_search = await make_search(db_session, await make_user(db_session))
    await make_search_job(db_session, other_search, idempotency_key="click-1")
    await make_search_job(db_session, search, idempotency_key=None, **done)
    await make_search_job(db_session, search, idempotency_key=None, **done)


async def test_a_search_has_at_most_one_active_run(db_session: AsyncSession) -> None:
    search = await make_search(db_session, await make_user(db_session))
    await make_search_job(db_session, search)  # pending

    for status in (SearchJobStatus.PENDING, SearchJobStatus.RUNNING):
        with pytest.raises(IntegrityError) as error:
            async with db_session.begin_nested():
                await make_search_job(db_session, search, status=status)
        assert violated_constraint(error.value) == "uq_search_jobs_active_search_id"

    await make_search_job(db_session, search, status=SearchJobStatus.FAILED)  # finished is fine


async def test_vacancy_can_be_saved_once_per_user(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    vacancy = await make_vacancy(db_session, await make_source(db_session))
    db_session.add(SavedVacancy(user_id=user.id, vacancy_id=vacancy.id))
    await db_session.flush()

    with pytest.raises(IntegrityError) as error:
        async with db_session.begin_nested():
            db_session.add(SavedVacancy(user_id=user.id, vacancy_id=vacancy.id))
            await db_session.flush()
    assert violated_constraint(error.value) == "uq_saved_vacancies_user_id_vacancy_id"


async def test_numeric_ranges_on_matches_and_jobs(db_session: AsyncSession) -> None:
    job = await make_search_job(
        db_session, await make_search(db_session, await make_user(db_session))
    )

    await expect_violation(
        db_session,
        "ck_search_jobs_result_count_non_negative",
        "UPDATE search_jobs SET result_count = -1 WHERE id = :id",
        job.id,
    )
    await expect_violation(
        db_session,
        "ck_search_jobs_finished_after_started",
        "UPDATE search_jobs SET started_at = now(), finished_at = now() - interval '1 minute' "
        "WHERE id = :id",
        job.id,
    )
    assert (
        await db_session.scalar(select(SearchJob.result_count).where(SearchJob.id == job.id)) == 0
    )


async def test_profile_salary_is_stored_exactly(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    profile = UserProfile(
        user_id=user.id,
        salary_min=Decimal("1500.50"),
        salary_max=Decimal("2500"),
        salary_currency="EUR",
    )
    db_session.add(profile)
    await db_session.flush()
    await db_session.refresh(profile)

    assert profile.salary_min == Decimal("1500.50")
