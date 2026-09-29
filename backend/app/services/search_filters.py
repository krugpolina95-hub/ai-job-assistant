"""How search filters reach results (architecture §10). No I/O: pure, deterministic rules.

For every active filter and every source there is exactly one mode (never silent):
- SOURCE: sent in the source's query; its results are trusted for that filter.
- LOCAL: checked here on the stored, normalized vacancy. A clear mismatch excludes the
  vacancy. An unknown value (e.g. no salary published) keeps it, marked unverified.
- UNSUPPORTED: the source never provides the data, so every result stays unverified.
Unverified filters are reported per result and per source; they are never passed off as
matches.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.core.enums import FilterMode, SalaryPeriod, SearchFilter
from app.models import Vacancy
from app.schemas.search import SalaryParams, SearchParamsV1
from app.services import normalization

# Reference rates to EUR for comparing salaries in different currencies (TD-14). Only EUR is
# known until the FX table lands (M5.11): other cross-currency comparisons stay unverified.
EUR_RATES: dict[str, Decimal] = {"EUR": Decimal(1)}


@dataclass(frozen=True)
class FilterDecision:
    include: bool
    excluded_by: SearchFilter | None
    unverified: tuple[SearchFilter, ...]


def active_filters(params: SearchParamsV1) -> list[SearchFilter]:
    location = params.location
    salary = params.salary
    candidates = {
        SearchFilter.KEYWORDS: bool(params.roles or params.keywords),
        SearchFilter.COUNTRY: bool(location and location.country),
        SearchFilter.REGION: bool(location and location.region),
        SearchFilter.CITY: bool(location and location.city),
        SearchFilter.RADIUS: bool(location and location.radius_km),
        SearchFilter.SALARY: bool(salary),
        SearchFilter.WORK_FORMATS: bool(params.work_formats),
        SearchFilter.EXPERIENCE_LEVELS: bool(params.experience_levels),
        SearchFilter.EMPLOYMENT_TYPES: bool(params.employment_types),
        SearchFilter.VACANCY_LANGUAGES: bool(params.vacancy_languages),
        SearchFilter.PUBLISHED_WITHIN_DAYS: params.published_within_days is not None,
    }
    return [search_filter for search_filter, active in candidates.items() if active]


def filter_modes(
    params: SearchParamsV1, support: Mapping[SearchFilter, FilterMode]
) -> dict[SearchFilter, FilterMode]:
    """Mode of each active filter for one source; undeclared filters are checked locally."""
    return {f: support.get(f, FilterMode.LOCAL) for f in active_filters(params)}


def source_params(params: SearchParamsV1) -> dict[str, Any]:
    """The query keys adapters read (see `SourceAdapter.search`)."""
    location = params.location
    return {
        "keywords": [*params.roles, *params.keywords],
        "country": location.country if location else None,
        "city": location.city if location else None,
        "published_within_days": params.published_within_days,
    }


def evaluate(
    vacancy: Vacancy,
    params: SearchParamsV1,
    modes: Mapping[SearchFilter, FilterMode],
    now: datetime,
) -> FilterDecision:
    unverified: list[SearchFilter] = []
    for search_filter, mode in modes.items():
        if mode is FilterMode.SOURCE:
            continue
        if mode is FilterMode.UNSUPPORTED:
            unverified.append(search_filter)
            continue
        matches = _CHECKS[search_filter](vacancy, params, now)
        if matches is False:
            return FilterDecision(include=False, excluded_by=search_filter, unverified=())
        if matches is None:
            unverified.append(search_filter)
    return FilterDecision(include=True, excluded_by=None, unverified=tuple(unverified))


# --- one check per filter: True = matches, False = clearly doesn't, None = unknown ----------


def _fold(text: str | None) -> str | None:
    return normalization.normalize_title(text) if text else None


def _keywords(vacancy: Vacancy, params: SearchParamsV1, _now: datetime) -> bool:
    haystack = " ".join(
        part or "" for part in (vacancy.title, vacancy.company, vacancy.description_snippet)
    )
    text = _fold(haystack) or ""
    return any((_fold(term) or "") in text for term in (*params.roles, *params.keywords))


def _same(requested: str | None, actual: str | None) -> bool | None:
    if actual is None:
        return None
    return _fold(requested) == _fold(actual)


def _country(vacancy: Vacancy, params: SearchParamsV1, _now: datetime) -> bool | None:
    return _same(params.location.country if params.location else None, vacancy.country)


def _region(vacancy: Vacancy, params: SearchParamsV1, _now: datetime) -> bool | None:
    return _same(params.location.region if params.location else None, vacancy.region)


def _city(vacancy: Vacancy, params: SearchParamsV1, _now: datetime) -> bool | None:
    return _same(params.location.city if params.location else None, vacancy.city)


def _radius(_vacancy: Vacancy, _params: SearchParamsV1, _now: datetime) -> bool | None:
    return None  # no coordinates are stored (TD-15): a distance can't be checked


def _member(actual: Any, allowed: Sequence[Any]) -> bool | None:
    return None if actual is None else actual in allowed


def _work_formats(vacancy: Vacancy, params: SearchParamsV1, _now: datetime) -> bool | None:
    return _member(vacancy.remote_type, params.work_formats)


def _levels(vacancy: Vacancy, params: SearchParamsV1, _now: datetime) -> bool | None:
    return _member(vacancy.experience_level, params.experience_levels)


def _employment(vacancy: Vacancy, params: SearchParamsV1, _now: datetime) -> bool | None:
    return _member(vacancy.employment_type, params.employment_types)


def _languages(vacancy: Vacancy, params: SearchParamsV1, _now: datetime) -> bool | None:
    return _member(vacancy.language, params.vacancy_languages)


def _published(vacancy: Vacancy, params: SearchParamsV1, now: datetime) -> bool | None:
    if vacancy.published_at is None or params.published_within_days is None:
        return None
    return vacancy.published_at >= now - timedelta(days=params.published_within_days)


def _salary(vacancy: Vacancy, params: SearchParamsV1, _now: datetime) -> bool | None:
    wanted = params.salary
    if wanted is None:
        return None
    offered_low = vacancy.salary_min if vacancy.salary_min is not None else vacancy.salary_max
    offered_high = vacancy.salary_max if vacancy.salary_max is not None else vacancy.salary_min
    if offered_low is None or offered_high is None or vacancy.salary_currency is None:
        return None
    low, high = _comparable(offered_low, offered_high, vacancy, wanted)
    wanted_min, wanted_max = _comparable_wanted(wanted, vacancy.salary_currency)
    if low is None or high is None or (wanted_min is None and wanted_max is None):
        return None
    if wanted_min is not None and high < wanted_min:
        return False
    return not (wanted_max is not None and low > wanted_max)


def _comparable(
    low: Decimal, high: Decimal, vacancy: Vacancy, wanted: SalaryParams
) -> tuple[Decimal | None, Decimal | None]:
    period = SalaryPeriod(vacancy.salary_period) if vacancy.salary_period else None
    if vacancy.salary_currency == wanted.currency:
        return normalization.to_monthly(low, period), normalization.to_monthly(high, period)
    currency = vacancy.salary_currency
    return (
        normalization.to_monthly_eur(low, currency, period, EUR_RATES),
        normalization.to_monthly_eur(high, currency, period, EUR_RATES),
    )


def _comparable_wanted(
    wanted: SalaryParams, offered_currency: str
) -> tuple[Decimal | None, Decimal | None]:
    if wanted.currency == offered_currency:
        return (
            normalization.to_monthly(wanted.min, wanted.period),
            normalization.to_monthly(wanted.max, wanted.period),
        )
    return (
        normalization.to_monthly_eur(wanted.min, wanted.currency, wanted.period, EUR_RATES),
        normalization.to_monthly_eur(wanted.max, wanted.currency, wanted.period, EUR_RATES),
    )


_CHECKS = {
    SearchFilter.KEYWORDS: _keywords,
    SearchFilter.COUNTRY: _country,
    SearchFilter.REGION: _region,
    SearchFilter.CITY: _city,
    SearchFilter.RADIUS: _radius,
    SearchFilter.SALARY: _salary,
    SearchFilter.WORK_FORMATS: _work_formats,
    SearchFilter.EXPERIENCE_LEVELS: _levels,
    SearchFilter.EMPLOYMENT_TYPES: _employment,
    SearchFilter.VACANCY_LANGUAGES: _languages,
    SearchFilter.PUBLISHED_WITHIN_DAYS: _published,
}
