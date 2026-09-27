"""Source adapter port (SOURCE-001, TD-08).

Every job source is one adapter class. The adapter is the only code that knows a source's
format: it turns raw records into `VacancyIn`, and the rest of the system never sees raw data.
Adapters only run for sources that are registered, enabled and compliance-reviewed, only call
their own allow-listed hosts, send an honest User-Agent, and never bypass CAPTCHAs, logins or
rate limits (SOURCE-003, CLAUDE.md §7).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.schemas.vacancy import VacancyIn

RawVacancy = Mapping[str, Any]


@dataclass(frozen=True)
class RateLimit:
    """Requests the source allows per time window (enforced by a token bucket later)."""

    requests: int
    per_seconds: int


@runtime_checkable
class SourceAdapter(Protocol):
    key: str
    """Stable identifier, equal to `sources.adapter_key` (get_source_name in SOURCE-001)."""

    def rate_limits(self) -> RateLimit: ...

    async def search(self, params: Mapping[str, Any]) -> list[RawVacancy]:
        """Fetch raw records for search parameters. `params` becomes the canonical
        `SearchParamsV1` (TD-10) when search runs are implemented."""
        ...

    def normalize(self, raw: RawVacancy) -> VacancyIn:
        """Map one raw record to `VacancyIn`. Raises `ValueError` (including Pydantic's
        `ValidationError`) for records that can't be mapped."""
        ...

    def validate(self, vacancy: VacancyIn) -> list[str]:
        """Source-specific checks on top of `VacancyIn` validation. Returns problems;
        an empty list means the record is accepted."""
        ...
