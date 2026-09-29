"""The whole source pipeline against a real database, with Adzuna mocked (respx):

external source → adapter → normalizer → validation → deduplication → database → API
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import SearchJobSourceStatus, StoragePolicy
from app.core.errors import NotFoundError, SourceNotEnabledError
from app.integrations.sources.adzuna.adapter import BASE_URL
from app.models import Source, Vacancy
from app.services.source_admin_service import ComplianceReviewRequiredError, SourceAdminService
from app.services.source_run_service import SourceRunService
from tests.conftest import build_test_settings
from tests.integration.db.factories import show_to_user
from tests.integration.db.search_helpers import ScriptedLimiter

pytestmark = pytest.mark.asyncio(loop_scope="session")

FIXTURE = Path(__file__).parents[2] / "fixtures" / "sources" / "adzuna" / "search_gb.json"
PAGE_1 = f"{BASE_URL}/gb/search/1"
CONFIGURED = build_test_settings(
    sources_contact="ops@example.com", adzuna_app_id="id", adzuna_app_key="secret-key"
)
QUERY = {"country": "GB", "keywords": ["developer"]}


def adzuna_page() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return payload


async def enabled_sources(db: AsyncSession, *keys: str) -> None:
    admin = SourceAdminService(db)
    await admin.sync_definitions()
    for key in keys:
        await admin.enable(key, compliance_review_confirmed=True)


async def adzuna_vacancies(db: AsyncSession) -> dict[str, Vacancy]:
    result = await db.scalars(select(Vacancy).join(Source).where(Source.adapter_key == "adzuna"))
    return {v.external_id or "": v for v in result}


# --- registration and the compliance gate -------------------------------------------------


async def test_sync_registers_sources_disabled_with_their_terms(db_session: AsyncSession) -> None:
    created = await SourceAdminService(db_session).sync_definitions()

    source = await db_session.scalar(select(Source).where(Source.adapter_key == "adzuna"))
    assert "adzuna" in created
    assert source is not None
    assert (source.is_enabled, source.compliance_reviewed_at) == (False, None)
    assert source.storage_policy is StoragePolicy.SNIPPET
    assert source.attribution_text == "Jobs by Adzuna"
    assert source.rate_limit == {"requests": 25, "per_seconds": 60, "requests_per_day": 250}


async def test_resync_keeps_enablement(db_session: AsyncSession) -> None:
    await enabled_sources(db_session, "adzuna")

    assert await SourceAdminService(db_session).sync_definitions() == []

    source = await db_session.scalar(select(Source).where(Source.adapter_key == "adzuna"))
    assert source is not None and source.is_enabled


async def test_enabling_requires_a_confirmed_compliance_review(db_session: AsyncSession) -> None:
    admin = SourceAdminService(db_session)
    await admin.sync_definitions()

    with pytest.raises(ComplianceReviewRequiredError):
        await admin.enable("adzuna", compliance_review_confirmed=False)
    source = await admin.enable("adzuna", compliance_review_confirmed=True)
    assert source.is_enabled and source.compliance_reviewed_at is not None

    reviewed_at = source.compliance_reviewed_at
    await admin.disable("adzuna")
    source = await admin.enable("adzuna", compliance_review_confirmed=False)  # review on file
    assert source.compliance_reviewed_at == reviewed_at


async def test_unknown_source_cannot_be_enabled(db_session: AsyncSession) -> None:
    await SourceAdminService(db_session).sync_definitions()

    with pytest.raises(NotFoundError):
        await SourceAdminService(db_session).enable("linkedin", compliance_review_confirmed=True)


@pytest.mark.parametrize("key", ["adzuna", "linkedin"])
async def test_disabled_or_unknown_source_makes_no_request(
    db_session: AsyncSession, key: str
) -> None:
    await SourceAdminService(db_session).sync_definitions()  # adzuna registered, not enabled

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith="https://")
        with pytest.raises(SourceNotEnabledError):
            await SourceRunService(db_session, CONFIGURED, ScriptedLimiter()).run(key, QUERY)

    assert not route.called


# --- a successful run ---------------------------------------------------------------------


async def test_adzuna_run_imports_normalized_vacancies(db_session: AsyncSession) -> None:
    await enabled_sources(db_session, "adzuna")

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(PAGE_1).respond(json=adzuna_page())
        result = await SourceRunService(db_session, CONFIGURED, ScriptedLimiter()).run(
            "adzuna", QUERY
        )

    assert result.status is SearchJobSourceStatus.SUCCEEDED
    # 6 results: 4004 links outside Adzuna (validation), 4005 has no link (normalizer).
    assert (result.fetched, result.created, result.skipped) == (6, 4, 2)
    request = route.calls.last.request
    assert request.headers["User-Agent"] == "AIJobAssistant/0.1 (+ops@example.com)"

    stored = await adzuna_vacancies(db_session)
    assert set(stored) == {"4001", "4002", "4003", "4006"}
    developer = stored["4001"]
    assert developer.title == "Python Developer"  # markup removed by the shared normalizer
    assert developer.description is None  # storage policy: snippet only
    assert developer.description_snippet is not None
    assert "<b>" not in developer.description_snippet
    assert (developer.salary_currency, developer.salary_period) == ("GBP", "year")
    assert developer.salary_monthly_min_eur is None  # GBP not comparable until FX (M5.11)
    assert stored["4002"].salary_min is None  # Adzuna estimate dropped


async def test_rerun_updates_instead_of_duplicating(db_session: AsyncSession) -> None:
    await enabled_sources(db_session, "adzuna")
    service = SourceRunService(db_session, CONFIGURED, ScriptedLimiter())

    with respx.mock() as mock:
        mock.get(PAGE_1).respond(json=adzuna_page())
        first = await service.run("adzuna", QUERY)
        second = await service.run("adzuna", QUERY)

    assert (first.created, second.created, second.updated) == (4, 0, 4)
    assert len(await adzuna_vacancies(db_session)) == 4


async def test_imported_vacancies_use_the_internal_model_in_the_api(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    registered = await api_client.post(
        "/api/v1/auth/register",
        json={"email": "adzuna-user@example.com", "password": "correct horse battery",
              "accept_terms": True},
    )  # fmt: skip
    await enabled_sources(db_session, "adzuna")
    with respx.mock() as mock:
        mock.get(PAGE_1).respond(json=adzuna_page())
        result = await SourceRunService(db_session, CONFIGURED, ScriptedLimiter()).run(
            "adzuna", QUERY
        )
    stored = await adzuna_vacancies(db_session)
    await show_to_user(db_session, registered.json()["id"], list(stored.values()))

    items = (await api_client.get("/api/v1/vacancies", params={"source": "adzuna"})).json()
    by_title = {item["title"]: item for item in items["items"]}

    assert len(by_title) == result.created
    developer = by_title["Python Developer"]
    assert developer["source"] == {
        "key": "adzuna",
        "name": "Adzuna",
        "attribution_text": "Jobs by Adzuna",
    }
    assert developer["url"].startswith("https://www.adzuna.co.uk/jobs/land/ad/4001")
    assert developer["salary"]["currency"] == "GBP"
    assert by_title["Data Analyst"]["salary"] is None  # "Not specified", not the estimate


async def test_another_source_runs_through_the_same_pipeline(db_session: AsyncSession) -> None:
    await enabled_sources(db_session, "mock")

    result = await SourceRunService(db_session, CONFIGURED, ScriptedLimiter()).run(
        "mock", {"country": "LT"}
    )

    assert result.status is SearchJobSourceStatus.SUCCEEDED
    assert result.created > 0


# --- failures are results, not crashes ------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "status", "code"),
    [
        (httpx.Response(500), SearchJobSourceStatus.FAILED, "SOURCE_UNAVAILABLE"),
        (httpx.Response(401), SearchJobSourceStatus.FAILED, "SOURCE_AUTH_FAILED"),
        (httpx.Response(429), SearchJobSourceStatus.FAILED, "SOURCE_RATE_LIMITED"),
        (httpx.ReadTimeout("slow"), SearchJobSourceStatus.TIMED_OUT, "SOURCE_TIMEOUT"),
    ],
)
async def test_source_failure_is_reported_and_stores_nothing(
    db_session: AsyncSession, response: Any, status: SearchJobSourceStatus, code: str
) -> None:
    await enabled_sources(db_session, "adzuna")

    with respx.mock() as mock:
        route = mock.get(PAGE_1)
        if isinstance(response, Exception):
            route.mock(side_effect=response)
        else:
            route.mock(return_value=response)
        result = await SourceRunService(db_session, CONFIGURED, ScriptedLimiter()).run(
            "adzuna", QUERY
        )

    assert (result.status, result.error_code) == (status, code)
    assert result.error_message and "secret-key" not in result.error_message
    assert await adzuna_vacancies(db_session) == {}


async def test_missing_configuration_fails_without_a_request(db_session: AsyncSession) -> None:
    await enabled_sources(db_session, "adzuna")
    unconfigured: Settings = build_test_settings(sources_contact="ops@example.com")

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=BASE_URL)
        result = await SourceRunService(db_session, unconfigured, ScriptedLimiter()).run(
            "adzuna", QUERY
        )

    assert (result.status, result.error_code) == (
        SearchJobSourceStatus.FAILED,
        "SOURCE_NOT_CONFIGURED",
    )
    assert not route.called
