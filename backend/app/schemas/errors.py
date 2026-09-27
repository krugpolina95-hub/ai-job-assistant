from typing import Any

from app.core.errors import ErrorCode
from app.schemas.base import OutputSchema


class FieldErrorOut(OutputSchema):
    """One request validation problem. The rejected input value is never echoed back."""

    loc: list[str | int]
    type: str
    message: str


class ErrorBodyOut(OutputSchema):
    code: ErrorCode
    message: str
    details: list[FieldErrorOut] | list[dict[str, Any]] | dict[str, Any] | None = None
    request_id: str | None = None


class ErrorOut(OutputSchema):
    error: ErrorBodyOut
