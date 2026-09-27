"""Vacancy schemas.

`VacancyIn` is the one source-independent shape every adapter must produce. Nothing
downstream (dedup, storage, API) knows about a source's own format.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.core.enums import EmploymentType, ExperienceLevel, SalaryPeriod, WorkFormat
from app.schemas.base import InputSchema, OutputSchema

ListItem = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]

# Tolerated clock skew between a source and us for publication dates.
MAX_PUBLISHED_AHEAD = timedelta(days=1)


def _upper(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


class VacancyIn(BaseModel):
    """A normalized posting produced by a source adapter. Unknown data stays None
    (VACANCY-003): adapters must not guess missing values."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    external_id: str | None = Field(default=None, min_length=1, max_length=255)
    url: str = Field(max_length=2048)
    title: str = Field(min_length=1, max_length=300)
    company: str | None = Field(default=None, max_length=300)
    description: str | None = Field(
        default=None,
        max_length=100_000,
        description="Plain text or HTML; always converted to plain text on ingest.",
    )
    requirements: list[ListItem] = Field(default_factory=list, max_length=100)
    responsibilities: list[ListItem] = Field(default_factory=list, max_length=100)
    skills: list[ListItem] = Field(default_factory=list, max_length=100)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")

    salary_min: Money | None = None
    salary_max: Money | None = None
    salary_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    salary_period: SalaryPeriod | None = None
    salary_raw: str | None = Field(default=None, max_length=200)

    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    region: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=200)
    location_raw: str | None = Field(default=None, max_length=300)
    remote_type: WorkFormat | None = None
    employment_type: EmploymentType | None = None
    experience_level: ExperienceLevel | None = None

    published_at: AwareDatetime | None = None
    is_closed: bool = Field(default=False, description="The source reports the posting closed.")

    _upper_codes = field_validator("salary_currency", "country", mode="before")(_upper)

    @field_validator("url")
    @classmethod
    def http_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("URL must be an absolute http(s) URL")
        return value

    @field_validator("published_at")
    @classmethod
    def not_in_future(cls, value: datetime | None) -> datetime | None:
        if value is not None and value > datetime.now(UTC) + MAX_PUBLISHED_AHEAD:
            raise ValueError("Publication date is in the future")
        return value

    @model_validator(mode="after")
    def consistent_salary(self) -> Self:
        low, high = self.salary_min, self.salary_max
        if low is not None and high is not None and low > high:
            raise ValueError("salary_min must not exceed salary_max")
        if (low is not None or high is not None) and self.salary_currency is None:
            raise ValueError("salary_currency is required when a salary amount is given")
        return self


# --- API: list parameters -----------------------------------------------------------------


class VacancySort(StrEnum):
    DATE = "date"  # newest publication first; unknown dates last
    SALARY = "salary"  # highest comparable monthly EUR salary first; unknown last


class VacancyListParams(InputSchema):
    q: str | None = Field(
        default=None, min_length=2, max_length=100, description="Text in title or company."
    )
    source: list[Annotated[str, StringConstraints(max_length=64)]] = Field(
        default_factory=list, max_length=20, description="Source keys."
    )
    remote_type: list[WorkFormat] = Field(default_factory=list)
    employment_type: list[EmploymentType] = Field(default_factory=list)
    experience_level: list[ExperienceLevel] = Field(default_factory=list)
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    city: str | None = Field(default=None, min_length=1, max_length=100)
    salary_min_eur: int | None = Field(
        default=None,
        ge=0,
        le=10_000_000,
        description="Minimum monthly salary in EUR. Vacancies without a comparable salary "
        "are excluded while this filter is set.",
    )
    published_within_days: int | None = Field(default=None, ge=1, le=365)
    include_inactive: bool = False
    sort: VacancySort = VacancySort.DATE
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=25, ge=1, le=100)

    _upper_country = field_validator("country", mode="before")(_upper)


# --- API: responses -----------------------------------------------------------------------


class FreshnessStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"  # old publication or not re-verified recently (AMB-11)
    REMOVED = "removed"  # the source reports it closed


class SourceOut(OutputSchema):
    key: str
    name: str
    attribution_text: str | None


class SalaryOut(OutputSchema):
    """Original values as published. `null` salary means "Not specified" (VACANCY-003)."""

    min: Decimal | None
    max: Decimal | None
    currency: str | None
    period: SalaryPeriod | None


class LocationOut(OutputSchema):
    country: str | None
    region: str | None
    city: str | None


class DuplicateOut(OutputSchema):
    """The same posting on another source (TD-16): "Also on …"."""

    source: SourceOut
    url: str


class VacancySummaryOut(OutputSchema):
    id: uuid.UUID
    title: str
    company: str | None
    location: LocationOut
    remote_type: WorkFormat | None
    employment_type: EmploymentType | None
    experience_level: ExperienceLevel | None
    salary: SalaryOut | None
    description_snippet: str | None
    url: str = Field(description="Original posting URL (VACANCY-002).")
    source: SourceOut
    published_at: datetime | None
    collected_at: datetime
    last_verified_at: datetime | None
    freshness: FreshnessStatus
    also_on: list[DuplicateOut]


class VacancyDetailOut(VacancySummaryOut):
    description: str | None = Field(
        description="Full text only when the source's storage policy allows it."
    )
    requirements: list[str]
    responsibilities: list[str]
    skills: list[str]
    language: str | None
    salary_raw: str | None
    location_raw: str | None
    removed_at: datetime | None


class VacancyPageOut(OutputSchema):
    items: list[VacancySummaryOut]
    next_cursor: str | None
