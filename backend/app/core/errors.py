"""Error catalog (GAP-11, TD-19).

Services raise these domain exceptions; `app/api/errors.py` turns them into the response
envelope `{"error": {"code", "message", "details", "request_id"}}`. The frontend translates
`code`, so `message` is English developer text and never shown to users as-is.

This module must not import FastAPI or Starlette: services depend on it.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from http import HTTPStatus
from typing import Any, ClassVar


class ErrorCode(StrEnum):
    BAD_REQUEST = "BAD_REQUEST"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    CSRF_FAILED = "CSRF_FAILED"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CONFLICT = "CONFLICT"
    EMAIL_ALREADY_REGISTERED = "EMAIL_ALREADY_REGISTERED"
    SOURCE_NOT_ENABLED = "SOURCE_NOT_ENABLED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


ErrorDetails = Mapping[str, Any] | Sequence[Mapping[str, Any]] | None


class AppError(Exception):
    """Base for every expected, client-visible error. Subclasses fix code and status."""

    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR
    status_code: ClassVar[HTTPStatus] = HTTPStatus.INTERNAL_SERVER_ERROR
    default_message: ClassVar[str] = "Internal server error"

    def __init__(self, message: str | None = None, *, details: ErrorDetails = None) -> None:
        self.message = message or self.default_message
        self.details = details
        super().__init__(self.message)


class NotFoundError(AppError):
    """Missing resource, and also a resource owned by another user (404, never 403)."""

    code = ErrorCode.NOT_FOUND
    status_code = HTTPStatus.NOT_FOUND
    default_message = "Resource not found"


class ConflictError(AppError):
    code = ErrorCode.CONFLICT
    status_code = HTTPStatus.CONFLICT
    default_message = "Request conflicts with the current state of the resource"


class ValidationFailedError(AppError):
    """A business-rule validation failure detected in a service (not request parsing)."""

    code = ErrorCode.VALIDATION_ERROR
    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    default_message = "Validation failed"


class UnauthenticatedError(AppError):
    """No valid session. Deliberately says nothing about why (missing, expired, revoked)."""

    code = ErrorCode.UNAUTHENTICATED
    status_code = HTTPStatus.UNAUTHORIZED
    default_message = "Authentication required"


class InvalidCredentialsError(AppError):
    """Same response for an unknown email and a wrong password (AUTH acceptance criteria)."""

    code = ErrorCode.INVALID_CREDENTIALS
    status_code = HTTPStatus.UNAUTHORIZED
    default_message = "Invalid email or password"


class CsrfFailedError(AppError):
    code = ErrorCode.CSRF_FAILED
    status_code = HTTPStatus.FORBIDDEN
    default_message = "CSRF token missing or invalid"


class EmailAlreadyRegisteredError(AppError):
    """Registration rejects duplicates explicitly (requirements-analysis F1). This reveals
    that an email is registered; registration rate limits (M3.5) bound that enumeration."""

    code = ErrorCode.EMAIL_ALREADY_REGISTERED
    status_code = HTTPStatus.CONFLICT
    default_message = "An account with this email already exists"


class SourceNotEnabledError(AppError):
    """Ingestion for a source that is unknown or disabled. Sources run only after their
    compliance review (TD-08, CLAUDE.md §7)."""

    code = ErrorCode.SOURCE_NOT_ENABLED
    status_code = HTTPStatus.CONFLICT
    default_message = "Source is not registered or not enabled"
