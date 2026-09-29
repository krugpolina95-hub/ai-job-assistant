"""Search parameters (TD-10), searches and search runs."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.core.enums import (
    EmploymentType,
    ExperienceLevel,
    FilterMode,
    LocationSource,
    SalaryPeriod,
    SearchChannel,
    SearchFilter,
    SearchJobSourceStatus,
    SearchJobStatus,
    SearchStatus,
    WorkFormat,
)
from app.schemas.base import InputSchema, OutputSchema
from app.schemas.vacancy import VacancySummaryOut

Term = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]


def _unique[T](values: list[T]) -> list[T]:
    return list(dict.fromkeys(values))


def _upper(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


class SalaryParams(InputSchema):
    min: Money | None = None
    max: Money | None = None
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    period: SalaryPeriod = SalaryPeriod.MONTH

    _upper_currency = field_validator("currency", mode="before")(_upper)

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        if self.min is None and self.max is None:
            raise ValueError("Give a minimum or a maximum salary")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("Minimum salary must not exceed maximum salary")
        if self.period is SalaryPeriod.UNKNOWN:
            raise ValueError("Choose a salary period")
        return self


class LocationParams(InputSchema):
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    region: str | None = Field(default=None, min_length=1, max_length=100)
    city: str | None = Field(default=None, min_length=1, max_length=100)
    radius_km: int | None = Field(default=None, ge=1, le=500)
    source: LocationSource | None = None
    confidence: Literal["high", "medium", "low"] | None = None

    _upper_country = field_validator("country", mode="before")(_upper)

    @model_validator(mode="after")
    def radius_needs_a_place(self) -> Self:
        if self.radius_km is not None and self.city is None:
            raise ValueError("A radius needs a city")
        return self


class SearchParamsV1(InputSchema):
    """The one canonical search definition for the form and the AI path (TD-10)."""

    roles: list[Term] = Field(min_length=1, max_length=10, description="At least one role.")
    keywords: list[Term] = Field(default_factory=list, max_length=20)
    salary: SalaryParams | None = None
    location: LocationParams | None = None
    work_formats: list[WorkFormat] = Field(default_factory=list, max_length=3)
    experience_levels: list[ExperienceLevel] = Field(default_factory=list, max_length=5)
    employment_types: list[EmploymentType] = Field(default_factory=list, max_length=5)
    skills: list[Term] = Field(
        default_factory=list, max_length=50, description="Used for matching, not as a filter."
    )
    vacancy_languages: list[Annotated[str, StringConstraints(pattern=r"^[a-z]{2}$")]] = Field(
        default_factory=list, max_length=10
    )
    published_within_days: int | None = Field(default=None, ge=1, le=365)
    relocation_ready: bool | None = Field(
        default=None, description="Used for matching, not as a filter."
    )

    @field_validator(
        "roles",
        "keywords",
        "work_formats",
        "experience_levels",
        "employment_types",
        "skills",
        "vacancy_languages",
    )
    @classmethod
    def deduplicate(cls, values: list[object]) -> list[object]:
        return _unique(values)


PARAMS_SCHEMA_VERSION = 1


class SearchIn(InputSchema):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    params: SearchParamsV1


class SearchOut(OutputSchema):
    id: uuid.UUID
    title: str | None
    params: SearchParamsV1
    params_schema_version: int
    channel: SearchChannel
    status: SearchStatus
    created_at: datetime
    updated_at: datetime


# --- search runs ----------------------------------------------------------------------------


class FilterOutcomeOut(OutputSchema):
    mode: FilterMode
    excluded: int = Field(description="Results dropped because they clearly don't match.")
    unverified: int = Field(description="Results kept although this filter couldn't be checked.")


class JobSourceOut(OutputSchema):
    key: str
    name: str
    status: SearchJobSourceStatus
    attempts: int
    fetched_count: int
    stored_count: int
    error_code: str | None = Field(description="Typed, sanitized reason; never a raw message.")
    filters: dict[SearchFilter, FilterOutcomeOut]


class FilterWarningOut(OutputSchema):
    """A requested filter that some results could not be checked against (never silent)."""

    filter: SearchFilter
    unsupported_by: list[str] = Field(description="Sources that never provide this data.")
    unverified_results: int


class JobProgressOut(OutputSchema):
    sources_total: int
    sources_done: int
    results_so_far: int


class SearchJobOut(OutputSchema):
    id: uuid.UUID
    search_id: uuid.UUID
    status: SearchJobStatus
    progress: JobProgressOut
    result_count: int
    error_code: str | None
    sources: list[JobSourceOut]
    filter_warnings: list[FilterWarningOut]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    deadline_at: datetime


class SearchJobListOut(OutputSchema):
    items: list[SearchJobOut]
    next_cursor: str | None


class JobResultOut(VacancySummaryOut):
    unverified_filters: list[SearchFilter] = Field(
        description="Requested filters this vacancy could not be checked against."
    )


class JobResultPageOut(OutputSchema):
    items: list[JobResultOut]
    next_cursor: str | None
