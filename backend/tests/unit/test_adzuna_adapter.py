"""Adzuna adapter against mocked HTTP responses (respx). No request leaves the machine."""

import json
import logging
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.core.enums import EmploymentType, SalaryPeriod
from app.core.logging import configure_logging
from app.integrations.sources.adzuna.adapter import BASE_URL, PAGE_SIZE, AdzunaAdapter
from app.integrations.sources.base import (
    MAX_RESULTS_PER_RUN,
    SourceAdapter,
    SourceAuthError,
    SourceError,
    SourceNotConfiguredError,
    SourceRateLimitedError,
    SourceResponseError,
    SourceTimeoutError,
    SourceUnavailableError,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "sources" / "adzuna" / "search_gb.json"
APP_ID = "test-app-id"
APP_KEY = "super-secret-adzuna-key"
PAGE_1 = f"{BASE_URL}/gb/search/1"


def fixture_payload() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return payload


def page_of(count: int, start: int = 0) -> dict[str, Any]:
    template = fixture_payload()["results"][0]
    return {"results": [template | {"id": str(start + n)} for n in range(count)]}


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def adapter(http: httpx.AsyncClient) -> AdzunaAdapter:
    return AdzunaAdapter(app_id=APP_ID, app_key=APP_KEY, http=http)


async def records_by_id(adapter: AdzunaAdapter) -> dict[str, Any]:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(PAGE_1).respond(json=fixture_payload())
        records = await adapter.search({"country": "GB"})
    return {record["id"]: record for record in records}


# --- the port ------------------------------------------------------------------------------


def test_adapter_implements_the_source_port(adapter: AdzunaAdapter) -> None:
    assert isinstance(adapter, SourceAdapter)
    assert adapter.key == "adzuna"
    assert (adapter.rate_limits().requests, adapter.rate_limits().requests_per_day) == (25, 250)


# --- requests ------------------------------------------------------------------------------


async def test_search_request_uses_the_official_api_and_query(adapter: AdzunaAdapter) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(PAGE_1).respond(json={"results": []})

        await adapter.search(
            {"country": "gb", "keywords": ["python", "developer"], "city": "London",
             "published_within_days": 7}
        )  # fmt: skip

    request = route.calls.last.request
    assert request.url.host == "api.adzuna.com"
    assert request.url.scheme == "https"
    assert dict(request.url.params) == {
        "app_id": APP_ID,
        "app_key": APP_KEY,
        "results_per_page": str(PAGE_SIZE),
        "what": "python developer",
        "where": "London",
        "max_days_old": "7",
    }


async def test_search_pages_until_a_short_page(adapter: AdzunaAdapter) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(PAGE_1).respond(json=page_of(PAGE_SIZE))
        mock.get(f"{BASE_URL}/gb/search/2").respond(json=page_of(3, start=PAGE_SIZE))

        records = await adapter.search({"country": "GB", "roles": ["python"]})

    assert len(records) == PAGE_SIZE + 3


async def test_search_never_exceeds_the_per_run_cap(adapter: AdzunaAdapter) -> None:
    with respx.mock(assert_all_called=False) as mock:
        pages = [
            mock.get(f"{BASE_URL}/gb/search/{page}").respond(json=page_of(PAGE_SIZE, page * 100))
            for page in (1, 2)
        ]
        third = mock.get(f"{BASE_URL}/gb/search/3").respond(json=page_of(PAGE_SIZE))

        records = await adapter.search({"country": "GB"})

    assert len(records) == MAX_RESULTS_PER_RUN
    assert all(route.called for route in pages)
    assert not third.called


async def test_uncovered_country_makes_no_request(adapter: AdzunaAdapter) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=BASE_URL)

        assert await adapter.search({"country": "LT"}) == []
        assert await adapter.search({}) == []

    assert not route.called


async def test_missing_credentials_fail_before_any_request(http: httpx.AsyncClient) -> None:
    adapter = AdzunaAdapter(app_id=APP_ID, app_key=None, http=http)

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=BASE_URL)
        with pytest.raises(SourceNotConfiguredError):
            await adapter.search({"country": "GB"})

    assert not route.called


# --- error mapping -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(401), SourceAuthError),
        (httpx.Response(403), SourceAuthError),
        (httpx.Response(429), SourceRateLimitedError),
        (httpx.Response(500), SourceUnavailableError),
        (httpx.Response(503), SourceUnavailableError),
        (httpx.Response(404), SourceResponseError),
        (httpx.Response(200, text="<html>not json</html>"), SourceResponseError),
        (httpx.Response(200, json={"unexpected": True}), SourceResponseError),
        (httpx.Response(200, json={"results": ["not an object"]}), SourceResponseError),
        (httpx.Response(200, json=[1, 2]), SourceResponseError),
    ],
)
async def test_http_failures_map_to_source_errors(
    adapter: AdzunaAdapter, response: httpx.Response, error: type[SourceError]
) -> None:
    with respx.mock() as mock:
        mock.get(PAGE_1).mock(return_value=response)
        with pytest.raises(error) as raised:
            await adapter.search({"country": "GB"})

    assert APP_KEY not in str(raised.value)


@pytest.mark.parametrize(
    ("exception", "error"),
    [
        (httpx.ReadTimeout("timed out"), SourceTimeoutError),
        (httpx.ConnectTimeout("timed out"), SourceTimeoutError),
        (httpx.ConnectError("refused"), SourceUnavailableError),
    ],
)
async def test_network_failures_map_to_source_errors_without_leaking_the_url(
    adapter: AdzunaAdapter, exception: Exception, error: type[SourceError]
) -> None:
    with respx.mock() as mock:
        mock.get(PAGE_1).mock(side_effect=exception)
        with pytest.raises(error) as raised:
            await adapter.search({"country": "GB"})

    assert raised.value.__cause__ is None  # the httpx error (with the keyed URL) is dropped
    assert raised.value.__suppress_context__
    assert APP_KEY not in str(raised.value)


async def test_api_key_never_reaches_the_logs(
    adapter: AdzunaAdapter, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG", "json")
    try:
        with respx.mock() as mock:
            mock.get(PAGE_1).respond(json={"results": []})
            await adapter.search({"country": "GB"})
        output = capsys.readouterr().out
    finally:
        logging.getLogger().handlers.clear()

    assert APP_KEY not in output
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# --- mapping to VacancyIn ------------------------------------------------------------------


async def test_normalize_maps_adzuna_fields(adapter: AdzunaAdapter) -> None:
    raw = (await records_by_id(adapter))["4001"]

    vacancy = adapter.normalize(raw)

    assert vacancy.external_id == "4001"
    assert vacancy.url == "https://www.adzuna.co.uk/jobs/land/ad/4001?se=abc123&v=xyz"
    assert vacancy.company == "Acme Ltd"
    assert (vacancy.salary_min, vacancy.salary_max) == (Decimal("55000.00"), Decimal("70000.00"))
    assert (vacancy.salary_currency, vacancy.salary_period) == ("GBP", SalaryPeriod.YEAR)
    assert (vacancy.country, vacancy.region, vacancy.city) == ("GB", "London", "Central London")
    assert vacancy.location_raw == "Central London, London"
    assert vacancy.employment_type is EmploymentType.FULL_TIME
    assert vacancy.published_at is not None and vacancy.published_at.isoformat() == (
        "2026-09-20T10:15:00+00:00"
    )
    assert vacancy.remote_type is None  # not provided by Adzuna: never guessed


async def test_predicted_salary_is_dropped(adapter: AdzunaAdapter) -> None:
    vacancy = adapter.normalize((await records_by_id(adapter))["4002"])

    assert vacancy.salary_min is None and vacancy.salary_max is None
    assert vacancy.salary_currency is None and vacancy.salary_period is None
    assert vacancy.employment_type is EmploymentType.CONTRACT


async def test_missing_optional_fields_stay_empty(adapter: AdzunaAdapter) -> None:
    vacancy = adapter.normalize((await records_by_id(adapter))["4003"])

    assert vacancy.company is None
    assert vacancy.salary_min is None
    assert vacancy.published_at is None
    assert (vacancy.country, vacancy.region, vacancy.city) == ("GB", None, None)
    assert vacancy.employment_type is EmploymentType.PART_TIME


async def test_fractional_salary_is_rounded_to_cents(adapter: AdzunaAdapter) -> None:
    vacancy = adapter.normalize((await records_by_id(adapter))["4006"])

    assert vacancy.salary_min == Decimal("30500.56")
    assert vacancy.salary_max is None


async def test_record_without_link_cannot_be_normalized(adapter: AdzunaAdapter) -> None:
    with pytest.raises(KeyError):
        adapter.normalize((await records_by_id(adapter))["4005"])


async def test_validate_accepts_only_adzuna_links(adapter: AdzunaAdapter) -> None:
    records = await records_by_id(adapter)

    assert adapter.validate(adapter.normalize(records["4001"])) == []
    assert adapter.validate(adapter.normalize(records["4006"])) == []  # bare adzuna.co.uk
    assert adapter.validate(adapter.normalize(records["4004"])) == [
        "link does not point to an Adzuna domain"
    ]
