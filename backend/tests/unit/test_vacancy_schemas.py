from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.vacancy import VacancyIn, VacancyListParams, VacancySort

VALID: dict[str, Any] = {"url": "https://jobs.example.com/1", "title": "Python Developer"}


def test_minimal_vacancy_keeps_unknown_fields_empty() -> None:
    vacancy = VacancyIn(**VALID)

    assert vacancy.salary_min is None
    assert vacancy.published_at is None
    assert vacancy.skills == []
    assert vacancy.is_closed is False


def test_codes_are_normalized_to_upper_case() -> None:
    vacancy = VacancyIn(**VALID, salary_min=1, salary_currency="eur", country=" lt ")

    assert (vacancy.salary_currency, vacancy.country) == ("EUR", "LT")


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"url": "javascript:alert(1)"}, "url"),
        ({"url": "/relative/path"}, "url"),
        ({"url": "ftp://jobs.example.com/1"}, "url"),
        ({"title": ""}, "title"),
        ({"title": "x" * 301}, "title"),
        ({"salary_min": -1, "salary_currency": "EUR"}, "salary_min"),
        ({"salary_currency": "EURO"}, "salary_currency"),
        ({"country": "LTU"}, "country"),
        ({"language": "english"}, "language"),
        ({"remote_type": "sometimes"}, "remote_type"),
        ({"skills": ["x" * 301]}, "skills"),
        ({"published_at": datetime(2026, 1, 1)}, "published_at"),  # naive
        ({"published_at": datetime.now(UTC) + timedelta(days=3)}, "published_at"),
        ({"surprise": 1}, "surprise"),
    ],
)
def test_invalid_field_values_are_rejected(override: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError) as error:
        VacancyIn(**VALID | override)

    assert error.value.errors()[0]["loc"][0] == field


@pytest.mark.parametrize(
    "override",
    [
        {"salary_min": Decimal(5000), "salary_max": Decimal(3000), "salary_currency": "EUR"},
        {"salary_min": Decimal(3000)},  # amount without currency
    ],
)
def test_inconsistent_salary_is_rejected(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        VacancyIn(**VALID | override)


def test_list_params_defaults() -> None:
    params = VacancyListParams()

    assert params.sort is VacancySort.DATE
    assert params.limit == 25
    assert params.include_inactive is False


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 101}, "limit"),
        ({"q": "x"}, "q"),
        ({"salary_min_eur": -5}, "salary_min_eur"),
        ({"published_within_days": 0}, "published_within_days"),
        ({"remote_type": ["anywhere"]}, "remote_type"),
        ({"sort": "match"}, "sort"),
        ({"country": "LTU"}, "country"),
        ({"source": ["s"] * 21}, "source"),
        ({"page": 2}, "page"),
    ],
)
def test_invalid_list_params_are_rejected(override: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError) as error:
        VacancyListParams(**override)

    assert error.value.errors()[0]["loc"][0] == field
