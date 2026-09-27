"""Vacancy list and details over HTTP (TD-13, TD-16, SOURCE-004, VACANCY-003)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import EmploymentType, ExperienceLevel, SalaryPeriod, StoragePolicy, WorkFormat
from app.core.ids import uuid7
from app.models import SavedVacancy, Source, Vacancy
from tests.integration.db.factories import (
    make_enabled_source,
    make_user,
    make_vacancy,
    show_to_user,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime.now(UTC)


async def sign_in(client: AsyncClient) -> uuid.UUID:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"user-{uuid7().hex}@example.com",
            "password": "correct horse battery",
            "accept_terms": True,
        },
    )
    assert response.status_code == 201
    return uuid.UUID(response.json()["id"])


async def vacancy(db: AsyncSession, source: Source, **values: Any) -> Vacancy:
    defaults: dict[str, Any] = {
        "collected_at": NOW,
        "last_verified_at": NOW,
        "published_at": NOW - timedelta(days=1),
        "company": "Acme",
        "description_snippet": "Build things.",
    }
    return await make_vacancy(db, source, **defaults | values)


async def list_ids(client: AsyncClient, **params: Any) -> list[str]:
    response = await client.get("/api/v1/vacancies", params=params)
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def ids(*vacancies: Vacancy) -> list[str]:
    return [str(v.id) for v in vacancies]


# --- retrieval and visibility -------------------------------------------------------------


async def test_list_shows_only_vacancies_visible_to_the_user(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await sign_in(api_client)
    other_user = await make_user(db_session)
    source = await make_enabled_source(db_session)
    in_results = await vacancy(db_session, source)
    saved = await vacancy(db_session, source)
    someone_elses = await vacancy(db_session, source)
    unlinked = await vacancy(db_session, source)
    await show_to_user(db_session, user_id, [in_results])
    db_session.add(SavedVacancy(user_id=user_id, vacancy_id=saved.id))
    await show_to_user(db_session, other_user.id, [someone_elses])
    await db_session.flush()

    visible = set(await list_ids(api_client))

    assert visible == {str(in_results.id), str(saved.id)}
    assert str(unlinked.id) not in visible


async def test_list_is_empty_for_a_new_user(api_client: AsyncClient) -> None:
    await sign_in(api_client)

    response = await api_client.get("/api/v1/vacancies")

    assert response.json() == {"items": [], "next_cursor": None}


async def test_details_include_source_information_and_original_link(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await sign_in(api_client)
    source = await make_enabled_source(
        db_session, name="Mock Board", attribution_text="Jobs via Mock Board"
    )
    item = await vacancy(
        db_session,
        source,
        url="https://jobs.example.com/original?id=1",
        description="Full description.",
        skills=["Python"],
        salary_min=Decimal("3000"),
        salary_max=Decimal("4500"),
        salary_currency="EUR",
        salary_period=SalaryPeriod.MONTH,
    )
    await show_to_user(db_session, user_id, [item])

    response = await api_client.get(f"/api/v1/vacancies/{item.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == {
        "key": source.adapter_key,
        "name": "Mock Board",
        "attribution_text": "Jobs via Mock Board",
    }
    assert body["url"] == "https://jobs.example.com/original?id=1"  # never rewritten
    assert body["salary"] == {
        "min": "3000.00",
        "max": "4500.00",
        "currency": "EUR",
        "period": "month",
    }
    assert body["freshness"] == "fresh"
    assert body["collected_at"] and body["last_verified_at"]
    assert body["description"] == "Full description."
    assert body["skills"] == ["Python"]


async def test_details_do_not_expose_internal_fields(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await sign_in(api_client)
    item = await vacancy(db_session, await make_enabled_source(db_session))
    await show_to_user(db_session, user_id, [item])

    body = (await api_client.get(f"/api/v1/vacancies/{item.id}")).json()

    for internal in (
        "content_hash",
        "canonical_url",
        "normalized_title",
        "company_normalized",
        "duplicate_group_id",
        "salary_monthly_min_eur",
        "external_id",
        "source_id",
    ):
        assert internal not in body


async def test_missing_salary_is_null_not_estimated(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await sign_in(api_client)
    item = await vacancy(db_session, await make_enabled_source(db_session), published_at=None)
    await show_to_user(db_session, user_id, [item])

    body = (await api_client.get(f"/api/v1/vacancies/{item.id}")).json()

    assert body["salary"] is None
    assert body["published_at"] is None


@pytest.mark.parametrize(
    ("values", "freshness"),
    [
        ({"published_at": NOW - timedelta(days=40)}, "stale"),
        ({"last_verified_at": NOW - timedelta(days=10)}, "stale"),
        ({"is_active": False, "removed_at": NOW}, "removed"),
    ],
)
async def test_stale_and_removed_postings_are_flagged(
    api_client: AsyncClient, db_session: AsyncSession, values: dict[str, Any], freshness: str
) -> None:
    user_id = await sign_in(api_client)
    item = await vacancy(db_session, await make_enabled_source(db_session), **values)
    await show_to_user(db_session, user_id, [item])

    body = (await api_client.get(f"/api/v1/vacancies/{item.id}")).json()

    assert body["freshness"] == freshness


async def test_description_follows_the_source_storage_policy(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await sign_in(api_client)
    source = await make_enabled_source(db_session, storage_policy=StoragePolicy.SNIPPET)
    item = await vacancy(db_session, source, description=None, description_snippet="Short.")
    await show_to_user(db_session, user_id, [item])

    body = (await api_client.get(f"/api/v1/vacancies/{item.id}")).json()

    assert body["description"] is None
    assert body["description_snippet"] == "Short."


async def test_invisible_or_unknown_vacancy_is_not_found(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    await sign_in(api_client)
    other_user = await make_user(db_session)
    foreign = await vacancy(db_session, await make_enabled_source(db_session))
    await show_to_user(db_session, other_user.id, [foreign])

    for vacancy_id in (foreign.id, uuid7()):
        response = await api_client.get(f"/api/v1/vacancies/{vacancy_id}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_malformed_vacancy_id_is_a_validation_error(api_client: AsyncClient) -> None:
    await sign_in(api_client)

    response = await api_client.get("/api/v1/vacancies/not-a-uuid")

    assert response.status_code == 422


# --- filtering ----------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def catalog(api_client: AsyncClient, db_session: AsyncSession) -> dict[str, Vacancy]:
    user_id = await sign_in(api_client)
    main = await make_enabled_source(db_session, "board-a")
    other = await make_enabled_source(db_session, "board-b")
    items = {
        "vilnius_python": await vacancy(
            db_session, main, title="Python Developer", country="LT", city="Vilnius",
            remote_type=WorkFormat.HYBRID, employment_type=EmploymentType.FULL_TIME,
            experience_level=ExperienceLevel.MIDDLE, salary_min=Decimal(3000),
            salary_max=Decimal(4500), salary_currency="EUR", salary_period=SalaryPeriod.MONTH,
            salary_monthly_min_eur=Decimal(3000), salary_monthly_max_eur=Decimal(4500),
            published_at=NOW - timedelta(days=2),
        ),
        "remote_qa": await vacancy(
            db_session, main, title="QA Engineer", company="Globex",
            remote_type=WorkFormat.REMOTE, experience_level=ExperienceLevel.JUNIOR,
            published_at=None,
        ),
        "berlin_contract": await vacancy(
            db_session, other, title="DevOps Contractor", country="DE", city="Berlin",
            remote_type=WorkFormat.REMOTE, employment_type=EmploymentType.CONTRACT,
            experience_level=ExperienceLevel.SENIOR, salary_min=Decimal(50),
            salary_max=Decimal(70), salary_currency="EUR", salary_period=SalaryPeriod.HOUR,
            salary_monthly_min_eur=Decimal(8400), salary_monthly_max_eur=Decimal(11760),
            published_at=NOW - timedelta(days=20),
        ),
        "usd_analyst": await vacancy(
            db_session, other, title="Data Analyst", company="Initech", country="US",
            city="New York", remote_type=WorkFormat.ONSITE, salary_min=Decimal(60000),
            salary_max=Decimal(80000), salary_currency="USD", salary_period=SalaryPeriod.YEAR,
            published_at=NOW - timedelta(days=5),
        ),
        "closed": await vacancy(
            db_session, main, title="Closed Python Role", is_active=False, removed_at=NOW,
        ),
    }  # fmt: skip
    await show_to_user(db_session, user_id, list(items.values()))
    return items


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, {"vilnius_python", "remote_qa", "berlin_contract", "usd_analyst"}),
        (
            {"include_inactive": "true"},
            {"vilnius_python", "remote_qa", "berlin_contract", "usd_analyst", "closed"},
        ),
        ({"q": "python"}, {"vilnius_python"}),
        ({"q": "GLOBEX"}, {"remote_qa"}),
        ({"source": "board-b"}, {"berlin_contract", "usd_analyst"}),
        ({"remote_type": "remote"}, {"remote_qa", "berlin_contract"}),
        ({"remote_type": ["hybrid", "onsite"]}, {"vilnius_python", "usd_analyst"}),
        ({"employment_type": "contract"}, {"berlin_contract"}),
        ({"experience_level": ["junior", "senior"]}, {"remote_qa", "berlin_contract"}),
        ({"country": "lt"}, {"vilnius_python"}),
        ({"city": "new york"}, {"usd_analyst"}),
        ({"salary_min_eur": 4000}, {"vilnius_python", "berlin_contract"}),
        ({"salary_min_eur": 5000}, {"berlin_contract"}),
        ({"published_within_days": 7}, {"vilnius_python", "usd_analyst"}),
        ({"remote_type": "remote", "salary_min_eur": 1}, {"berlin_contract"}),
        ({"q": "100%_match"}, set()),
    ],
)
async def test_filters(
    api_client: AsyncClient,
    catalog: dict[str, Vacancy],
    params: dict[str, Any],
    expected: set[str],
) -> None:
    by_id = {str(v.id): name for name, v in catalog.items()}

    found = {by_id[vacancy_id] for vacancy_id in await list_ids(api_client, **params)}

    assert found == expected


# --- sorting and pagination ---------------------------------------------------------------


async def test_sort_by_date_puts_unknown_dates_last(
    api_client: AsyncClient, catalog: dict[str, Vacancy]
) -> None:
    order = await list_ids(api_client, sort="date")

    assert order == ids(
        catalog["vilnius_python"],
        catalog["usd_analyst"],
        catalog["berlin_contract"],
        catalog["remote_qa"],
    )


async def test_sort_by_salary_uses_comparable_monthly_eur(
    api_client: AsyncClient, catalog: dict[str, Vacancy]
) -> None:
    order = await list_ids(api_client, sort="salary")

    assert order[:2] == ids(catalog["berlin_contract"], catalog["vilnius_python"])
    assert set(order[2:]) == set(ids(catalog["remote_qa"], catalog["usd_analyst"]))  # unknown


@pytest.mark.parametrize("sort", ["date", "salary"])
async def test_cursor_pagination_walks_every_item_exactly_once(
    api_client: AsyncClient, db_session: AsyncSession, sort: str
) -> None:
    user_id = await sign_in(api_client)
    source = await make_enabled_source(db_session)
    same_day = NOW - timedelta(days=3)
    items = [
        await vacancy(
            db_session,
            source,
            # Ties and missing values on purpose: the id breaks ties, unknowns come last.
            published_at=None if i % 4 == 0 else same_day - timedelta(hours=i // 2),
            salary_monthly_max_eur=None if i % 3 == 0 else Decimal(1000 + 100 * (i // 2)),
        )
        for i in range(11)
    ]
    await show_to_user(db_session, user_id, items)
    everything = await list_ids(api_client, sort=sort, limit=100)

    pages: list[list[str]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"sort": sort, "limit": 4} | ({"cursor": cursor} if cursor else {})
        body = (await api_client.get("/api/v1/vacancies", params=params)).json()
        pages.append([item["id"] for item in body["items"]])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert [len(page) for page in pages] == [4, 4, 3]
    assert [vacancy_id for page in pages for vacancy_id in page] == everything
    assert sorted(everything) == sorted(ids(*items))


@pytest.mark.parametrize(
    "cursor", ["garbage!", "eyJzb3J0IjoiZGF0ZSJ9", "eyJzb3J0IjoiZGF0ZSIsImlkIjoieCJ9"]
)
async def test_invalid_cursor_is_rejected(api_client: AsyncClient, cursor: str) -> None:
    await sign_in(api_client)

    response = await api_client.get("/api/v1/vacancies", params={"cursor": cursor})

    assert response.status_code == 422
    assert response.json()["error"]["details"] == {"field": "cursor"}


async def test_cursor_cannot_switch_sort_order(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await sign_in(api_client)
    source = await make_enabled_source(db_session)
    await show_to_user(db_session, user_id, [await vacancy(db_session, source) for _ in range(3)])
    first = (
        await api_client.get("/api/v1/vacancies", params={"sort": "salary", "limit": 1})
    ).json()

    response = await api_client.get(
        "/api/v1/vacancies", params={"sort": "date", "cursor": first["next_cursor"]}
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"sort": "match"},
        {"remote_type": "anywhere"},
        {"salary_min_eur": -1},
        {"q": "x"},
        {"unknown_filter": "1"},
    ],
)
async def test_invalid_list_parameters_are_rejected(
    api_client: AsyncClient, params: dict[str, Any]
) -> None:
    await sign_in(api_client)

    response = await api_client.get("/api/v1/vacancies", params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --- duplicates ---------------------------------------------------------------------------


async def test_duplicate_group_is_listed_once_with_other_sources(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await sign_in(api_client)
    board_a = await make_enabled_source(db_session, name="Board A")
    board_b = await make_enabled_source(db_session, name="Board B")
    sparse = await vacancy(db_session, board_a, company=None, description_snippet=None)
    complete = await vacancy(
        db_session, board_b, salary_min=Decimal(1), salary_currency="EUR", city="Vilnius"
    )
    sparse.duplicate_group_id = complete.duplicate_group_id = sparse.id
    await show_to_user(db_session, user_id, [sparse, complete])

    body = (await api_client.get("/api/v1/vacancies")).json()

    [item] = body["items"]
    assert item["id"] == str(complete.id)  # the most complete record represents the group
    assert item["also_on"] == [
        {
            "source": {"key": board_a.adapter_key, "name": "Board A", "attribution_text": None},
            "url": sparse.url,
        }
    ]


async def test_duplicate_detail_lists_the_other_source_even_if_not_in_results(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await sign_in(api_client)
    shown = await vacancy(db_session, await make_enabled_source(db_session))
    elsewhere = await vacancy(db_session, await make_enabled_source(db_session, name="Board C"))
    shown.duplicate_group_id = elsewhere.duplicate_group_id = shown.id
    await show_to_user(db_session, user_id, [shown])

    listed = await list_ids(api_client)
    detail = (await api_client.get(f"/api/v1/vacancies/{shown.id}")).json()

    assert listed == [str(shown.id)]
    assert [d["source"]["name"] for d in detail["also_on"]] == ["Board C"]
    assert (await api_client.get(f"/api/v1/vacancies/{elsewhere.id}")).status_code == 404
