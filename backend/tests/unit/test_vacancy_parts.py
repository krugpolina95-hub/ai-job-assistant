"""Pagination cursors, the mock adapter (port conformance) and the freshness rule."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core.enums import SalaryPeriod
from app.core.pagination import InvalidCursorError, decode_cursor, encode_cursor
from app.integrations.sources.base import SourceAdapter
from app.integrations.sources.mock.adapter import MockSourceAdapter, load_fixtures
from app.models import Vacancy
from app.schemas.vacancy import FreshnessStatus, VacancyIn
from app.services.vacancy_service import freshness

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


# --- cursors ------------------------------------------------------------------------------


def test_cursor_round_trip_is_url_safe() -> None:
    position = {"sort": "date", "value": "2026-09-01T00:00:00+00:00", "id": "abc"}

    cursor = encode_cursor(position)

    assert decode_cursor(cursor) == position
    assert all(char.isalnum() or char in "-_" for char in cursor)


@pytest.mark.parametrize("cursor", ["not base64!", "bm90IGpzb24", encode_cursor({}) + "x" * 600])
def test_malformed_cursor_is_a_validation_error(cursor: str) -> None:
    with pytest.raises(InvalidCursorError) as error:
        decode_cursor(cursor)

    assert error.value.code == "VALIDATION_ERROR"
    assert error.value.details == {"field": "cursor"}


def test_cursor_must_decode_to_an_object() -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(encode_cursor([1, 2]))  # type: ignore[arg-type]


# --- mock adapter -------------------------------------------------------------------------


def test_mock_adapter_implements_the_source_port() -> None:
    adapter = MockSourceAdapter()

    assert isinstance(adapter, SourceAdapter)
    assert adapter.key == "mock"
    assert adapter.rate_limits().requests > 0


async def test_mock_search_returns_copies_and_filters_by_role() -> None:
    adapter = MockSourceAdapter()

    everything = await adapter.search({})
    python_only = await adapter.search({"roles": ["python"]})
    everything[0]["position"] = "changed"

    assert len(everything) == len(load_fixtures())
    assert {record["id"] for record in python_only} == {"m-1", "m-1-repost"}
    assert (await adapter.search({}))[0]["position"] != "changed"


def test_mock_normalize_maps_its_own_format_to_vacancy_in() -> None:
    adapter = MockSourceAdapter()
    first = next(record for record in load_fixtures() if record["id"] == "m-1")

    vacancy = adapter.normalize(first)

    assert isinstance(vacancy, VacancyIn)
    assert vacancy.external_id == "m-1"
    assert (vacancy.salary_currency, vacancy.salary_period) == ("EUR", SalaryPeriod.MONTH)
    assert (vacancy.country, vacancy.city) == ("LT", "Vilnius")
    assert vacancy.published_at is not None
    assert vacancy.published_at.tzinfo is UTC


@pytest.mark.parametrize("record_id", ["m-6", "m-7"])
def test_mock_normalize_rejects_invalid_fixtures(record_id: str) -> None:
    record = next(r for r in load_fixtures() if r["id"] == record_id)

    with pytest.raises(ValidationError):
        MockSourceAdapter().normalize(record)


def test_mock_validate_applies_source_rules() -> None:
    adapter = MockSourceAdapter()

    assert adapter.validate(VacancyIn(url="https://x.test/1", title="QA")) == ["title too short"]
    assert adapter.validate(VacancyIn(url="https://x.test/1", title="QA Lead")) == []


# --- freshness (AMB-11) -------------------------------------------------------------------


def vacancy(**values: object) -> Vacancy:
    defaults: dict[str, object] = {
        "is_active": True,
        "collected_at": NOW - timedelta(days=1),
        "last_verified_at": NOW - timedelta(days=1),
        "published_at": NOW - timedelta(days=2),
    }
    return Vacancy(**defaults | values)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({}, FreshnessStatus.FRESH),
        ({"published_at": None}, FreshnessStatus.FRESH),
        ({"published_at": NOW - timedelta(days=31)}, FreshnessStatus.STALE),
        ({"last_verified_at": NOW - timedelta(days=8)}, FreshnessStatus.STALE),
        (
            {"last_verified_at": None, "collected_at": NOW - timedelta(days=8)},
            FreshnessStatus.STALE,
        ),
        ({"is_active": False}, FreshnessStatus.REMOVED),
    ],
)
def test_freshness(values: dict[str, object], expected: FreshnessStatus) -> None:
    assert freshness(vacancy(**values), NOW) is expected
