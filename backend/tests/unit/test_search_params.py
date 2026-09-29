from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.search import SearchParamsV1

VALID: dict[str, Any] = {"roles": ["Python Developer"]}


def test_minimal_params() -> None:
    params = SearchParamsV1(**VALID)

    assert params.roles == ["Python Developer"]
    assert params.salary is None and params.location is None


def test_lists_are_trimmed_and_deduplicated() -> None:
    params = SearchParamsV1(roles=[" QA ", "QA", "Tester"], work_formats=["remote", "remote"])

    assert params.roles == ["QA", "Tester"]
    assert params.work_formats == ["remote"]


def test_codes_are_upper_cased() -> None:
    params = SearchParamsV1(
        **VALID, salary={"min": 3000, "currency": "eur"}, location={"country": "lt"}
    )

    assert params.salary is not None and params.salary.currency == "EUR"
    assert params.location is not None and params.location.country == "LT"


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"roles": []}, "roles"),  # SEARCH-003: a role is required
        ({"roles": ["  "]}, "roles"),
        ({"salary": {"min": -1, "currency": "EUR"}}, "salary"),
        ({"salary": {"min": 5000, "max": 3000, "currency": "EUR"}}, "salary"),
        ({"salary": {"currency": "EUR"}}, "salary"),  # neither min nor max
        ({"salary": {"min": 3000}}, "salary"),  # no currency
        ({"salary": {"min": 3000, "currency": "EUR", "period": "unknown"}}, "salary"),
        ({"location": {"radius_km": 10}}, "location"),  # radius without a city
        ({"location": {"country": "Lithuania"}}, "location"),
        ({"work_formats": ["sometimes"]}, "work_formats"),
        ({"vacancy_languages": ["english"]}, "vacancy_languages"),
        ({"published_within_days": 0}, "published_within_days"),
        ({"unexpected": True}, "unexpected"),
    ],
)
def test_invalid_params_are_rejected(override: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError) as error:
        SearchParamsV1(**VALID | override)

    assert error.value.errors()[0]["loc"][0] == field
