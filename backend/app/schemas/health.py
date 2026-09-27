from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class CheckStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessOut(BaseModel):
    status: CheckStatus
    checks: dict[str, CheckStatus]
