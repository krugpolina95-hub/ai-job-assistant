from enum import StrEnum
from typing import Literal

from app.schemas.base import OutputSchema


class CheckStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"


class HealthOut(OutputSchema):
    status: Literal["ok"] = "ok"


class ReadinessOut(OutputSchema):
    status: CheckStatus
    checks: dict[str, CheckStatus]
