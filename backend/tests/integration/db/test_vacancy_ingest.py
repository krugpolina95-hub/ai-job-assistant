"""Creation/import through a source adapter, and duplicate handling (VACANCY-001, TD-16)."""

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import StoragePolicy
from app.core.errors import SourceNotEnabledError
from app.core.ids import uuid7
from app.integrations.sources.mock.adapter import MockSourceAdapter, load_fixtures
from app.models import Vacancy
from app.services.vacancy_ingest_service import VacancyIngestService
from tests.integration.db.factories import make_enabled_source, make_source

pytestmark = pytest.mark.asyncio(loop_scope="session")


def record(**overrides: Any) -> dict[str, Any]:
    """A raw record in the mock source's own format."""
    # Full hex: the first 12 hex digits of a UUIDv7 are only the millisecond timestamp.
    key = uuid7().hex
    return {
        "id": key,
        "link": f"https://jobs.mock.test/postings/{key}",
        "position": "Python Developer",
        "employer": "Acme Ltd",
        "html_body": f"Posting {key}.",
        "place": {"country": "LT", "city": "Vilnius"},
        "mode": "hybrid",
    } | overrides


async def vacancies_of(db: AsyncSession, source_id: object) -> list[Vacancy]:
    result = await db.scalars(
        select(Vacancy).where(Vacancy.source_id == source_id).order_by(Vacancy.collected_at)
    )
    return list(result)


async def by_external_id(db: AsyncSession, source_id: object, external_id: str) -> Vacancy:
    vacancy = await db.scalar(
        select(Vacancy).where(Vacancy.source_id == source_id, Vacancy.external_id == external_id)
    )
    assert vacancy is not None
    return vacancy


# --- creation / import --------------------------------------------------------------------


async def test_import_from_mock_adapter(db_session: AsyncSession) -> None:
    source = await make_enabled_source(db_session, "mock")
    adapter = MockSourceAdapter()

    result = await VacancyIngestService(db_session).ingest(adapter, await adapter.search({}))

    # 8 fixtures: 2 invalid, and "m-1-repost" is the same posting as "m-1" (same canonical URL).
    assert (result.created, result.updated, result.skipped) == (5, 1, 2)
    assert len(result.vacancy_ids) == 6
    assert len(await vacancies_of(db_session, source.id)) == 5
    assert any("record 5" in error for error in result.errors)  # m-6: empty title
    assert any("record 6" in error for error in result.errors)  # m-7: reversed salary


async def test_imported_fields_are_normalized_and_nothing_is_guessed(
    db_session: AsyncSession,
) -> None:
    source = await make_enabled_source(db_session, "mock")
    adapter = MockSourceAdapter()
    await VacancyIngestService(db_session).ingest(adapter, await adapter.search({}))

    analyst = await by_external_id(db_session, source.id, "m-3")
    assert analyst.description == "Dashboards and SQL."  # HTML → text
    assert analyst.company_normalized == "initech"
    assert (analyst.salary_currency, analyst.salary_monthly_max_eur) == ("USD", None)  # no FX yet

    qa = await by_external_id(db_session, source.id, "m-2")
    assert qa.salary_min is None and qa.salary_currency is None and qa.published_at is None

    contractor = await by_external_id(db_session, source.id, "m-8")
    assert contractor.salary_monthly_min_eur == Decimal("8400.00")  # 50 EUR/h * 168 h

    closed = await by_external_id(db_session, source.id, "m-5")
    assert closed.is_active is False
    assert closed.removed_at is not None


async def test_html_is_sanitized_on_import(db_session: AsyncSession) -> None:
    source = await make_enabled_source(db_session)
    body = "<p>Hi <b>there</b></p><script>steal()</script><img src=x onerror=alert(1)>"

    await VacancyIngestService(db_session).ingest(
        MockSourceAdapter(source.adapter_key), [record(id="h1", html_body=body)]
    )

    vacancy = await by_external_id(db_session, source.id, "h1")
    assert vacancy.description == "Hi there"
    assert vacancy.description_snippet == "Hi there"


@pytest.mark.parametrize(
    ("policy", "has_description", "has_snippet"),
    [
        (StoragePolicy.FULL, True, True),
        (StoragePolicy.SNIPPET, False, True),
        (StoragePolicy.NONE, False, False),
    ],
)
async def test_storage_policy_limits_what_is_stored(
    db_session: AsyncSession, policy: StoragePolicy, has_description: bool, has_snippet: bool
) -> None:
    source = await make_enabled_source(db_session, storage_policy=policy)

    await VacancyIngestService(db_session).ingest(
        MockSourceAdapter(source.adapter_key), [record(id="p1", html_body="Long text " * 100)]
    )

    vacancy = await by_external_id(db_session, source.id, "p1")
    assert (vacancy.description is not None) is has_description
    assert (vacancy.description_snippet is not None) is has_snippet
    if has_snippet:
        assert vacancy.description_snippet is not None
        assert len(vacancy.description_snippet) <= 500


async def test_disabled_or_unknown_source_is_refused(db_session: AsyncSession) -> None:
    disabled = await make_source(db_session)  # not enabled, never reviewed
    service = VacancyIngestService(db_session)

    for key in (disabled.adapter_key, "no-such-source"):
        with pytest.raises(SourceNotEnabledError):
            await service.ingest(MockSourceAdapter(key), [record()])

    assert await vacancies_of(db_session, disabled.id) == []


# --- validation ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"link": "javascript:alert(1)"},
        {"position": ""},
        {"position": "QA"},  # adapter.validate: title too short
        {"pay": {"from": 10, "to": 5, "cur": "EUR", "per": "month"}},
        {"pay": {"from": 10, "per": "month"}},  # amount without currency
        {"place": {"country": "Lithuania"}},
        {"link": None},
    ],
)
async def test_invalid_records_are_skipped_without_failing_the_batch(
    db_session: AsyncSession, bad: dict[str, Any]
) -> None:
    source = await make_enabled_source(db_session)
    adapter = MockSourceAdapter(source.adapter_key)

    result = await VacancyIngestService(db_session).ingest(
        adapter, [record(id="ok-1"), record(id="bad", **bad), record(id="ok-2")]
    )

    assert (result.created, result.skipped) == (2, 1)
    assert result.errors[0].startswith("record 1:")
    assert {v.external_id for v in await vacancies_of(db_session, source.id)} == {"ok-1", "ok-2"}


# --- duplicates ---------------------------------------------------------------------------


async def test_reimport_updates_instead_of_duplicating(db_session: AsyncSession) -> None:
    source = await make_enabled_source(db_session, "mock")
    adapter = MockSourceAdapter()
    service = VacancyIngestService(db_session)
    await service.ingest(adapter, await adapter.search({}))
    before = {v.id: v.last_verified_at for v in await vacancies_of(db_session, source.id)}

    again = await service.ingest(adapter, await adapter.search({}))

    after = await vacancies_of(db_session, source.id)
    assert (again.created, again.updated) == (0, 6)
    assert {v.id for v in after} == set(before)
    for vacancy in after:
        await db_session.refresh(vacancy)
        previous = before[vacancy.id]
        assert previous is not None and vacancy.last_verified_at is not None
        assert vacancy.last_verified_at > previous


async def test_same_external_id_updates_the_record(db_session: AsyncSession) -> None:
    source = await make_enabled_source(db_session)
    service = VacancyIngestService(db_session)
    adapter = MockSourceAdapter(source.adapter_key)
    await service.ingest(adapter, [record(id="x1", position="Python Developer")])

    result = await service.ingest(
        adapter,
        [record(id="x1", position="Senior Python Developer", link="https://jobs.mock.test/new")],
    )

    [vacancy] = await vacancies_of(db_session, source.id)
    assert (result.created, result.updated) == (0, 1)
    assert vacancy.title == "Senior Python Developer"
    assert vacancy.url == "https://jobs.mock.test/new"


async def test_same_url_without_external_id_is_the_same_record(db_session: AsyncSession) -> None:
    source = await make_enabled_source(db_session)
    service = VacancyIngestService(db_session)
    adapter = MockSourceAdapter(source.adapter_key)
    link = f"https://jobs.mock.test/postings/{uuid7().hex}"

    await service.ingest(adapter, [record(id=None, link=link)])
    result = await service.ingest(adapter, [record(id=None, link=f"{link}/?utm_medium=email")])

    assert result.updated == 1
    assert len(await vacancies_of(db_session, source.id)) == 1


async def test_same_posting_on_two_sources_is_grouped_not_merged(db_session: AsyncSession) -> None:
    first = await make_enabled_source(db_session)
    second = await make_enabled_source(db_session)
    service = VacancyIngestService(db_session)
    shared = record(link=f"https://careers.acme.test/jobs/{uuid7().hex}")

    await service.ingest(MockSourceAdapter(first.adapter_key), [shared])
    result = await service.ingest(MockSourceAdapter(second.adapter_key), [shared | {"id": "other"}])

    [original] = await vacancies_of(db_session, first.id)
    [copy] = await vacancies_of(db_session, second.id)
    assert (result.created, result.grouped) == (1, 1)
    assert original.id != copy.id
    assert original.duplicate_group_id == copy.duplicate_group_id == original.id


async def test_same_content_on_another_source_is_grouped(db_session: AsyncSession) -> None:
    first = await make_enabled_source(db_session)
    second = await make_enabled_source(db_session)
    service = VacancyIngestService(db_session)
    body = f"Identical text {uuid7().hex}."

    await service.ingest(MockSourceAdapter(first.adapter_key), [record(html_body=body)])
    await service.ingest(
        MockSourceAdapter(second.adapter_key),
        [record(html_body=f"<p>{body}</p>", employer="ACME")],  # formatting differs only
    )

    [original] = await vacancies_of(db_session, first.id)
    [copy] = await vacancies_of(db_session, second.id)
    assert copy.duplicate_group_id == original.id


@pytest.mark.parametrize(
    ("base", "changes", "grouped"),
    [
        # Same title, reworded description and other URL: content hash differs, fuzzy matches.
        ({}, {"employer": "ACME Inc."}, True),
        (
            {"position": "Senior Backend Python Developer"},
            {"position": "Senior Backend Python Developers"},
            True,
        ),
        # TD-16 threshold is 0.85; this pair scores 0.84, so it stays separate.
        ({}, {"position": "Python Developers"}, False),
        ({}, {"place": {"country": "LT", "city": "Kaunas"}}, False),
        ({}, {"employer": "Globex"}, False),
        ({}, {"mode": "remote"}, False),
        ({}, {"position": "Java Developer"}, False),
    ],
    ids=[
        "reworded",
        "similar-long-title",
        "below-threshold",
        "other-city",
        "other-company",
        "other-work-format",
        "other-title",
    ],
)
async def test_near_duplicate_is_grouped_only_when_title_company_and_place_match(
    db_session: AsyncSession, base: dict[str, Any], changes: dict[str, Any], grouped: bool
) -> None:
    first = await make_enabled_source(db_session)
    second = await make_enabled_source(db_session)
    service = VacancyIngestService(db_session)
    await service.ingest(MockSourceAdapter(first.adapter_key), [record(**base)])

    result = await service.ingest(MockSourceAdapter(second.adapter_key), [record(**base | changes)])

    [original] = await vacancies_of(db_session, first.id)
    [candidate] = await vacancies_of(db_session, second.id)
    assert result.grouped == int(grouped)
    assert (candidate.duplicate_group_id == original.id) is grouped


async def test_closed_posting_keeps_its_first_removal_time(db_session: AsyncSession) -> None:
    source = await make_enabled_source(db_session)
    service = VacancyIngestService(db_session)
    adapter = MockSourceAdapter(source.adapter_key)
    await service.ingest(adapter, [record(id="c1", closed=True)])
    [vacancy] = await vacancies_of(db_session, source.id)
    first_removed_at = vacancy.removed_at

    await service.ingest(adapter, [record(id="c1", closed=True)])
    await service.ingest(adapter, [record(id="c1")])  # reopened
    await db_session.refresh(vacancy)

    assert first_removed_at is not None
    assert vacancy.is_active is True and vacancy.removed_at is None


async def test_fixture_dataset_has_no_accidental_duplicates(db_session: AsyncSession) -> None:
    await make_enabled_source(db_session, "mock")
    adapter = MockSourceAdapter()
    await VacancyIngestService(db_session).ingest(adapter, await adapter.search({}))

    groups = await db_session.scalar(
        select(func.count()).select_from(Vacancy).where(Vacancy.duplicate_group_id.is_not(None))
    )
    assert groups == 0
    assert len(load_fixtures()) == 8
