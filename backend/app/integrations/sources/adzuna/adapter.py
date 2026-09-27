"""Adzuna job search API (source "adzuna"). Terms and limits: docs/source-compliance.md.

Official REST API, authenticated with an app id and key. Only this module knows Adzuna's
format. It fetches at most MAX_RESULTS_PER_RUN results from the single allow-listed host and
maps each to `VacancyIn`. Rules from Adzuna's data that matter here:
- `salary_is_predicted = 1` marks an Adzuna *estimate*. It is dropped: we never show guessed
  salaries (VACANCY-003).
- Salaries are annual amounts in the country's currency.
- `description` is a snippet only; the source's storage policy is `snippet` (TD-23).
- `redirect_url` is the only link Adzuna provides, and the one its terms expect us to use.
"""

import math
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from app.core.enums import EmploymentType, SalaryPeriod
from app.integrations.sources.base import (
    MAX_RESULTS_PER_RUN,
    RateLimit,
    RawVacancy,
    SourceAuthError,
    SourceNotConfiguredError,
    SourceRateLimitedError,
    SourceResponseError,
    SourceTimeoutError,
    SourceUnavailableError,
)
from app.schemas.vacancy import VacancyIn

API_HOST = "api.adzuna.com"
BASE_URL = f"https://{API_HOST}/v1/api/jobs"
PAGE_SIZE = 50  # Adzuna's maximum results_per_page

# Countries Adzuna covers: ISO 3166-1 alpha-2 → (path code, salary currency).
COUNTRIES: dict[str, tuple[str, str]] = {
    "GB": ("gb", "GBP"),
    "US": ("us", "USD"),
    "AT": ("at", "EUR"),
    "AU": ("au", "AUD"),
    "BE": ("be", "EUR"),
    "BR": ("br", "BRL"),
    "CA": ("ca", "CAD"),
    "CH": ("ch", "CHF"),
    "DE": ("de", "EUR"),
    "ES": ("es", "EUR"),
    "FR": ("fr", "EUR"),
    "IN": ("in", "INR"),
    "IT": ("it", "EUR"),
    "MX": ("mx", "MXN"),
    "NL": ("nl", "EUR"),
    "NZ": ("nz", "NZD"),
    "PL": ("pl", "PLN"),
    "SG": ("sg", "SGD"),
    "ZA": ("za", "ZAR"),
}

# Adzuna's own domains (adzuna.co.uk, www.adzuna.de, adzuna.com.au, ...).
_ADZUNA_LINK_HOST = re.compile(r"(?:[a-z0-9-]+\.)*adzuna\.[a-z]{2,3}(?:\.[a-z]{2})?")
_COUNTRY_FIELD = "__country__"  # added to each raw record: results don't carry the country
_CENTS = Decimal("0.01")


class AdzunaAdapter:
    key = "adzuna"
    RATE_LIMIT: ClassVar[RateLimit] = RateLimit(requests=25, per_seconds=60, requests_per_day=250)

    def __init__(self, *, app_id: str | None, app_key: str | None, http: httpx.AsyncClient) -> None:
        self._app_id = app_id
        self._app_key = app_key
        self._http = http

    def rate_limits(self) -> RateLimit:
        return self.RATE_LIMIT

    async def search(self, params: Mapping[str, Any]) -> list[RawVacancy]:
        if not self._app_id or not self._app_key:
            raise SourceNotConfiguredError("Adzuna credentials are not configured")
        country = str(params.get("country") or "").upper()
        if country not in COUNTRIES:
            return []  # Adzuna doesn't cover this country: nothing to ask for.
        path_code = COUNTRIES[country][0]

        query = _query(params) | {
            "app_id": self._app_id,
            "app_key": self._app_key,
            "results_per_page": PAGE_SIZE,
        }
        records: list[RawVacancy] = []
        for page in range(1, math.ceil(MAX_RESULTS_PER_RUN / PAGE_SIZE) + 1):
            results = await self._fetch_page(f"{BASE_URL}/{path_code}/search/{page}", query)
            records.extend({**result, _COUNTRY_FIELD: country} for result in results)
            if len(results) < PAGE_SIZE:
                break
        return records[:MAX_RESULTS_PER_RUN]

    async def _fetch_page(self, url: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        # Exceptions are re-raised `from None`: httpx errors include the request URL, and
        # with it the app key.
        try:
            response = await self._http.get(url, params=query)
        except httpx.TimeoutException:
            raise SourceTimeoutError("Adzuna request timed out") from None
        except httpx.HTTPError:
            raise SourceUnavailableError("Adzuna could not be reached") from None

        status = response.status_code
        if status in (401, 403):
            raise SourceAuthError(f"Adzuna rejected the credentials (HTTP {status})")
        if status == 429:
            raise SourceRateLimitedError("Adzuna rate limit reached (HTTP 429)")
        if status >= 500:
            raise SourceUnavailableError(f"Adzuna is unavailable (HTTP {status})")
        if status != 200:
            raise SourceResponseError(f"Adzuna returned HTTP {status}")
        try:
            payload = response.json()
        except ValueError:
            raise SourceResponseError("Adzuna returned invalid JSON") from None
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
            raise SourceResponseError("Adzuna response has no result list")
        return results

    def normalize(self, raw: RawVacancy) -> VacancyIn:
        country = raw[_COUNTRY_FIELD]
        predicted = str(raw.get("salary_is_predicted", "0")) == "1"
        salary_min = None if predicted else _money(raw.get("salary_min"))
        salary_max = None if predicted else _money(raw.get("salary_max"))
        has_salary = salary_min is not None or salary_max is not None

        location = raw.get("location") or {}
        area = [str(part) for part in location.get("area") or [] if part]
        company = raw.get("company") or {}

        return VacancyIn(
            external_id=str(raw["id"]),
            url=raw["redirect_url"],
            title=raw["title"],
            company=company.get("display_name"),
            description=raw.get("description"),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=COUNTRIES[country][1] if has_salary else None,
            salary_period=SalaryPeriod.YEAR if has_salary else None,
            country=country,
            # area = [country, region, ..., town]; the first element repeats the country.
            region=area[1] if len(area) >= 3 else None,
            city=area[-1] if len(area) >= 2 else None,
            location_raw=location.get("display_name"),
            employment_type=_employment_type(raw.get("contract_type"), raw.get("contract_time")),
            published_at=datetime.fromisoformat(raw["created"]) if raw.get("created") else None,
        )

    def validate(self, vacancy: VacancyIn) -> list[str]:
        host = (urlsplit(vacancy.url).hostname or "").lower()
        if not _ADZUNA_LINK_HOST.fullmatch(host):
            return ["link does not point to an Adzuna domain"]
        return []


def _query(params: Mapping[str, Any]) -> dict[str, Any]:
    keywords = params.get("keywords") or params.get("roles") or []
    query: dict[str, Any] = {}
    if keywords:
        query["what"] = " ".join(str(word) for word in keywords)
    if params.get("city"):
        query["where"] = str(params["city"])
    if params.get("published_within_days"):
        query["max_days_old"] = int(params["published_within_days"])
    return query


def _money(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(_CENTS)
    except InvalidOperation:
        raise ValueError(f"invalid salary amount {value!r}") from None


def _employment_type(contract_type: Any, contract_time: Any) -> EmploymentType | None:
    if contract_type == "contract":
        return EmploymentType.CONTRACT
    if contract_time == "full_time":
        return EmploymentType.FULL_TIME
    if contract_time == "part_time":
        return EmploymentType.PART_TIME
    return None
