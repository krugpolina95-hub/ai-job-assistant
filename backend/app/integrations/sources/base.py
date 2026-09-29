"""Source adapter port (SOURCE-001, TD-08).

Every job source is one adapter class. The adapter is the only code that knows a source's
format: it turns raw records into `VacancyIn`, and the rest of the system never sees raw data.
Adapters only run for sources that are registered, enabled and compliance-reviewed, only call
their own allow-listed hosts, send an honest User-Agent, and never bypass CAPTCHAs, logins or
rate limits (SOURCE-003, CLAUDE.md §7).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable

from app.core.enums import FilterMode, SearchFilter
from app.schemas.vacancy import VacancyIn

RawVacancy = Mapping[str, Any]


class SourceErrorCode(StrEnum):
    """Why a source call failed. Stored per source run; orchestration (n8n, TD-06) decides
    about retries from it: only UNAVAILABLE and TIMEOUT are worth retrying."""

    NOT_CONFIGURED = "SOURCE_NOT_CONFIGURED"
    AUTH_FAILED = "SOURCE_AUTH_FAILED"
    RATE_LIMITED = "SOURCE_RATE_LIMITED"
    UNAVAILABLE = "SOURCE_UNAVAILABLE"
    TIMEOUT = "SOURCE_TIMEOUT"
    BAD_RESPONSE = "SOURCE_BAD_RESPONSE"
    QUOTA_EXHAUSTED = "SOURCE_QUOTA_EXHAUSTED"
    RATE_LIMITER_UNAVAILABLE = "SOURCE_RATE_LIMITER_UNAVAILABLE"
    # Set by the search run itself, not by adapters:
    DEADLINE_EXCEEDED = "SOURCE_DEADLINE_EXCEEDED"
    DISABLED = "SOURCE_DISABLED"
    CANCELLED = "SOURCE_RUN_CANCELLED"
    UNEXPECTED = "SOURCE_UNEXPECTED_ERROR"


# Failures that may succeed if tried again soon. Everything else is final for this run.
RETRYABLE_SOURCE_ERRORS = frozenset(
    {
        SourceErrorCode.UNAVAILABLE,
        SourceErrorCode.TIMEOUT,
        SourceErrorCode.RATE_LIMITED,
        SourceErrorCode.RATE_LIMITER_UNAVAILABLE,
    }
)


class SourceError(Exception):
    """A failed call to an external source. Messages never contain credentials or full
    request URLs, so they are safe to log and store."""

    code: ClassVar[SourceErrorCode]

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SourceNotConfiguredError(SourceError):
    code = SourceErrorCode.NOT_CONFIGURED


class SourceAuthError(SourceError):
    code = SourceErrorCode.AUTH_FAILED


class SourceRateLimitedError(SourceError):
    code = SourceErrorCode.RATE_LIMITED


class SourceUnavailableError(SourceError):
    code = SourceErrorCode.UNAVAILABLE


class SourceTimeoutError(SourceError):
    code = SourceErrorCode.TIMEOUT


class SourceResponseError(SourceError):
    code = SourceErrorCode.BAD_RESPONSE


class SourceQuotaExhaustedError(SourceError):
    """The source's daily quota is used up: retrying today would only waste time."""

    code = SourceErrorCode.QUOTA_EXHAUSTED


class SourceRateLimiterUnavailableError(SourceError):
    """The shared limiter can't be reached. We fail closed rather than risk exceeding a
    provider's limits."""

    code = SourceErrorCode.RATE_LIMITER_UNAVAILABLE


# Architecture §10: at most this many raw results per source per run (MVP).
MAX_RESULTS_PER_RUN = 100


@dataclass(frozen=True)
class RateLimit:
    """Requests the source allows per time window (enforced by a token bucket later)."""

    requests: int
    per_seconds: int
    requests_per_day: int | None = None


@runtime_checkable
class SourceAdapter(Protocol):
    key: str
    """Stable identifier, equal to `sources.adapter_key` (get_source_name in SOURCE-001)."""

    filter_support: Mapping[SearchFilter, FilterMode]
    """How the adapter handles each search filter: SOURCE when it sends the filter in its
    query, UNSUPPORTED when the source never provides the data. Unlisted filters are
    checked LOCAL-ly after retrieval (see app/services/search_filters.py)."""

    def rate_limits(self) -> RateLimit: ...

    async def search(self, params: Mapping[str, Any]) -> list[RawVacancy]:
        """Fetch raw records for search parameters, at most `MAX_RESULTS_PER_RUN`. Raises a
        `SourceError` subclass on failure. `params` becomes the canonical `SearchParamsV1`
        (TD-10) when search runs are implemented; adapters read these keys today:
        `keywords` / `roles` (list[str]), `country` (ISO 3166-1 alpha-2), `city` (str),
        `published_within_days` (int)."""
        ...

    def normalize(self, raw: RawVacancy) -> VacancyIn:
        """Map one raw record to `VacancyIn`. Raises `ValueError` (including Pydantic's
        `ValidationError`) for records that can't be mapped."""
        ...

    def validate(self, vacancy: VacancyIn) -> list[str]:
        """Source-specific checks on top of `VacancyIn` validation. Returns problems;
        an empty list means the record is accepted."""
        ...
