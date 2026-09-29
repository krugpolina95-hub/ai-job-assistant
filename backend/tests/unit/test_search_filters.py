"""Filter handling (clarification 6): each filter is SOURCE, LOCAL or UNSUPPORTED, never silent."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.core.enums import (
    EmploymentType,
    ExperienceLevel,
    FilterMode,
    SalaryPeriod,
    SearchFilter,
    WorkFormat,
)
from app.integrations.sources.adzuna.adapter import AdzunaAdapter
from app.models import Vacancy
from app.schemas.search import SearchParamsV1
from app.services.search_filters import (
    FilterDecision,
    active_filters,
    evaluate,
    filter_modes,
    source_params,
)

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
LOCAL = FilterMode.LOCAL


def params(**values: Any) -> SearchParamsV1:
    return SearchParamsV1(**{"roles": ["python developer"]} | values)


def vacancy(**values: Any) -> Vacancy:
    defaults: dict[str, Any] = {"title": "Senior Python Developer", "company": "Acme"}
    return Vacancy(**defaults | values)


def decide(
    p: SearchParamsV1, v: Vacancy, modes: dict[SearchFilter, FilterMode] | None = None
) -> FilterDecision:
    return evaluate(v, p, modes if modes is not None else filter_modes(p, {}), NOW)


def test_only_requested_filters_are_active() -> None:
    assert active_filters(params()) == [SearchFilter.KEYWORDS]
    assert set(
        active_filters(
            params(
                location={"country": "LT", "city": "Vilnius", "radius_km": 20},
                salary={"min": 3000, "currency": "EUR"},
                work_formats=["remote"],
                published_within_days=7,
            )
        )
    ) == {
        SearchFilter.KEYWORDS,
        SearchFilter.COUNTRY,
        SearchFilter.CITY,
        SearchFilter.RADIUS,
        SearchFilter.SALARY,
        SearchFilter.WORK_FORMATS,
        SearchFilter.PUBLISHED_WITHIN_DAYS,
    }


def test_skills_and_relocation_are_not_filters() -> None:
    assert active_filters(params(skills=["SQL"], relocation_ready=True)) == [SearchFilter.KEYWORDS]


def test_undeclared_filters_default_to_local() -> None:
    p = params(work_formats=["remote"])

    assert filter_modes(p, {}) == {SearchFilter.KEYWORDS: LOCAL, SearchFilter.WORK_FORMATS: LOCAL}


def test_adzuna_declares_every_filter_it_cannot_check() -> None:
    p = params(
        location={"country": "GB", "city": "London", "radius_km": 5},
        work_formats=["remote"],
        experience_levels=["senior"],
        vacancy_languages=["en"],
        salary={"min": 40000, "currency": "GBP", "period": "year"},
        employment_types=["full_time"],
        published_within_days=7,
    )

    modes = filter_modes(p, AdzunaAdapter.filter_support)

    assert {f for f, m in modes.items() if m is FilterMode.UNSUPPORTED} == {
        SearchFilter.WORK_FORMATS,
        SearchFilter.EXPERIENCE_LEVELS,
        SearchFilter.VACANCY_LANGUAGES,
        SearchFilter.RADIUS,
    }
    assert {f for f, m in modes.items() if m is FilterMode.SOURCE} == {
        SearchFilter.KEYWORDS,
        SearchFilter.COUNTRY,
        SearchFilter.CITY,
        SearchFilter.PUBLISHED_WITHIN_DAYS,
    }
    assert modes[SearchFilter.SALARY] is LOCAL  # its salary filter would match estimates
    assert modes[SearchFilter.EMPLOYMENT_TYPES] is LOCAL


def test_source_params_use_the_adapter_keys() -> None:
    p = params(keywords=["fastapi"], location={"country": "GB", "city": "London"})

    assert source_params(p) == {
        "keywords": ["python developer", "fastapi"],
        "country": "GB",
        "city": "London",
        "published_within_days": None,
    }


# --- modes ----------------------------------------------------------------------------------


def test_source_mode_trusts_the_source() -> None:
    p = params(work_formats=["remote"])
    decision = decide(
        p, vacancy(remote_type=WorkFormat.ONSITE), {SearchFilter.WORK_FORMATS: FilterMode.SOURCE}
    )

    assert decision.include is True and decision.unverified == ()


def test_unsupported_mode_keeps_the_result_but_flags_it() -> None:
    p = params(work_formats=["remote"])
    decision = decide(
        p, vacancy(remote_type=None), {SearchFilter.WORK_FORMATS: FilterMode.UNSUPPORTED}
    )

    assert decision.include is True
    assert decision.unverified == (SearchFilter.WORK_FORMATS,)


# --- local checks: match / mismatch / unknown ----------------------------------------------


@pytest.mark.parametrize(
    ("request_values", "vacancy_values", "expected"),
    [
        ({"work_formats": ["remote"]}, {"remote_type": WorkFormat.REMOTE}, True),
        ({"work_formats": ["remote"]}, {"remote_type": WorkFormat.ONSITE}, False),
        ({"work_formats": ["remote"]}, {"remote_type": None}, None),
        ({"experience_levels": ["senior"]}, {"experience_level": ExperienceLevel.SENIOR}, True),
        ({"experience_levels": ["senior"]}, {"experience_level": ExperienceLevel.JUNIOR}, False),
        ({"experience_levels": ["senior"]}, {}, None),
        ({"employment_types": ["contract"]}, {"employment_type": EmploymentType.CONTRACT}, True),
        ({"employment_types": ["contract"]}, {"employment_type": EmploymentType.FULL_TIME}, False),
        ({"vacancy_languages": ["en"]}, {"language": "en"}, True),
        ({"vacancy_languages": ["en"]}, {"language": "de"}, False),
        ({"vacancy_languages": ["en"]}, {}, None),
        ({"location": {"country": "LT"}}, {"country": "LT"}, True),
        ({"location": {"country": "LT"}}, {"country": "DE"}, False),
        ({"location": {"country": "LT"}}, {}, None),
        ({"location": {"city": "Vilnius"}}, {"city": "VILNIUS"}, True),
        ({"location": {"city": "Vilnius"}}, {"city": "Kaunas"}, False),
        ({"location": {"region": "Vilnius County"}}, {}, None),
        ({"location": {"city": "Vilnius", "radius_km": 10}}, {"city": "Vilnius"}, None),
        ({"published_within_days": 7}, {"published_at": NOW - timedelta(days=2)}, True),
        ({"published_within_days": 7}, {"published_at": NOW - timedelta(days=9)}, False),
        ({"published_within_days": 7}, {"published_at": None}, None),
    ],
)
def test_local_checks(
    request_values: dict[str, Any], vacancy_values: dict[str, Any], expected: bool | None
) -> None:
    p = params(**request_values)
    checked = [f for f in active_filters(p) if f is not SearchFilter.KEYWORDS]

    decision = decide(p, vacancy(**vacancy_values))

    if expected is False:
        assert decision.include is False and decision.excluded_by in checked
    elif expected is None:
        assert decision.include is True and set(checked) & set(decision.unverified)
    else:
        assert decision.include is True and not set(checked) & set(decision.unverified)


def test_keywords_checked_locally_when_the_source_cannot() -> None:
    p = params(roles=["data analyst"], keywords=["sql"])

    assert decide(p, vacancy(title="Data Analyst")).include is True
    assert decide(p, vacancy(title="Chef", description_snippet="SQL skills")).include is True
    assert decide(p, vacancy(title="Chef", description_snippet="Cooking")).include is False


# --- salary --------------------------------------------------------------------------------


def paid(low: int | None, high: int | None, currency: str, period: SalaryPeriod) -> Vacancy:
    return vacancy(
        salary_min=Decimal(low) if low is not None else None,
        salary_max=Decimal(high) if high is not None else None,
        salary_currency=currency,
        salary_period=period,
    )


@pytest.mark.parametrize(
    ("wanted", "offered", "expected"),
    [
        ({"min": 4000, "currency": "EUR"}, paid(3000, 4500, "EUR", SalaryPeriod.MONTH), True),
        ({"min": 5000, "currency": "EUR"}, paid(3000, 4500, "EUR", SalaryPeriod.MONTH), False),
        ({"max": 2500, "currency": "EUR"}, paid(3000, 4500, "EUR", SalaryPeriod.MONTH), False),
        ({"min": 4000, "currency": "EUR"}, paid(50, 70, "EUR", SalaryPeriod.HOUR), True),
        (
            {"min": 48000, "currency": "EUR", "period": "year"},
            paid(4000, None, "EUR", SalaryPeriod.MONTH),
            True,
        ),
        ({"min": 3000, "currency": "GBP"}, paid(40000, 50000, "GBP", SalaryPeriod.YEAR), True),
        ({"min": 5000, "currency": "GBP"}, paid(40000, 50000, "GBP", SalaryPeriod.YEAR), False),
        # No FX yet (M5.11): a different currency can't be compared, so it is unverified.
        ({"min": 3000, "currency": "EUR"}, paid(60000, 80000, "USD", SalaryPeriod.YEAR), None),
        ({"min": 3000, "currency": "EUR"}, paid(3000, 4000, "EUR", SalaryPeriod.UNKNOWN), None),
        ({"min": 3000, "currency": "EUR"}, vacancy(), None),  # salary not published
    ],
)
def test_salary_filter(wanted: dict[str, Any], offered: Vacancy, expected: bool | None) -> None:
    decision = decide(params(salary=wanted), offered)

    if expected is None:
        assert decision.include and SearchFilter.SALARY in decision.unverified
    else:
        assert decision.include is expected
        assert SearchFilter.SALARY not in decision.unverified


def test_first_clear_mismatch_is_reported() -> None:
    p = params(work_formats=["remote"], experience_levels=["senior"])

    decision = decide(p, vacancy(remote_type=WorkFormat.ONSITE, experience_level=None))

    assert (decision.include, decision.excluded_by) == (False, SearchFilter.WORK_FORMATS)
