"""Offline mock source (TD-08): deterministic fixtures, no network.

Its raw format deliberately differs from `VacancyIn` (other field names, nested salary and
place, epoch timestamps, HTML bodies) so it exercises the same mapping a real adapter needs.
The fixtures cover SOURCE-002 cases: missing salary and date, several currencies and periods,
a closed posting, a re-posted duplicate, HTML with scripts, and invalid records.
"""

import copy
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.enums import FilterMode, SearchFilter
from app.integrations.sources.base import RateLimit, RawVacancy
from app.schemas.vacancy import VacancyIn

FIXTURES_PATH = Path(__file__).with_name("fixtures.json")
MIN_TITLE_LENGTH = 3


def load_fixtures() -> list[dict[str, Any]]:
    fixtures: list[dict[str, Any]] = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    return fixtures


class MockSourceAdapter:
    # search() matches roles/keywords against titles; everything else is in its records.
    filter_support: Mapping[SearchFilter, FilterMode] = {
        SearchFilter.KEYWORDS: FilterMode.SOURCE,
        SearchFilter.RADIUS: FilterMode.UNSUPPORTED,  # no coordinates
    }

    def __init__(self, key: str = "mock", records: Sequence[RawVacancy] | None = None) -> None:
        self.key = key
        self._records = list(records) if records is not None else load_fixtures()

    def rate_limits(self) -> RateLimit:
        return RateLimit(requests=60, per_seconds=60)

    async def search(self, params: Mapping[str, Any]) -> list[RawVacancy]:
        roles = [word.casefold() for word in params.get("keywords") or params.get("roles") or []]
        return [
            copy.deepcopy(record)
            for record in self._records
            if not roles
            or any(role in str(record.get("position", "")).casefold() for role in roles)
        ]

    def normalize(self, raw: RawVacancy) -> VacancyIn:
        pay = raw.get("pay") or {}
        place = raw.get("place") or {}
        posted = raw.get("posted")
        return VacancyIn(
            external_id=str(raw["id"]) if raw.get("id") is not None else None,
            url=raw["link"],
            title=raw.get("position") or "",
            company=raw.get("employer"),
            description=raw.get("html_body"),
            requirements=raw.get("must_have", []),
            skills=raw.get("tags", []),
            language=raw.get("lang"),
            salary_min=pay.get("from"),
            salary_max=pay.get("to"),
            salary_currency=pay.get("cur"),
            salary_period=pay.get("per"),
            salary_raw=pay.get("text"),
            country=place.get("country"),
            city=place.get("city"),
            location_raw=place.get("text"),
            remote_type=raw.get("mode"),
            employment_type=raw.get("contract"),
            experience_level=raw.get("level"),
            published_at=datetime.fromtimestamp(posted, tz=UTC) if posted else None,
            is_closed=bool(raw.get("closed", False)),
        )

    def validate(self, vacancy: VacancyIn) -> list[str]:
        if len(vacancy.title) < MIN_TITLE_LENGTH:
            return ["title too short"]
        return []
