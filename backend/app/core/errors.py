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
    INVALID_CURRENT_PASSWORD = "INVALID_CURRENT_PASSWORD"  # noqa: S105
    RESET_TOKEN_INVALID = "RESET_TOKEN_INVALID"  # noqa: S105
    CSRF_FAILED = "CSRF_FAILED"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    CONFLICT = "CONFLICT"
    EMAIL_ALREADY_REGISTERED = "EMAIL_ALREADY_REGISTERED"
    SOURCE_NOT_ENABLED = "SOURCE_NOT_ENABLED"
    SEARCH_NOT_CONFIRMED = "SEARCH_NOT_CONFIRMED"
    NO_SOURCES_ENABLED = "NO_SOURCES_ENABLED"
    JOB_LIMIT_REACHED = "JOB_LIMIT_REACHED"
    RATE_LIMITED = "RATE_LIMITED"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    JOB_ALREADY_FINISHED = "JOB_ALREADY_FINISHED"
    JOB_NOT_RUNNING = "JOB_NOT_RUNNING"
    AI_UNAVAILABLE = "AI_UNAVAILABLE"
    AI_INVALID_RESPONSE = "AI_INVALID_RESPONSE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


ErrorDetails = Mapping[str, Any] | Sequence[Mapping[str, Any]] | None


class AppError(Exception):
    """Base for every expected, client-visible error. Subclasses fix code and status."""

    code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR
    status_code: ClassVar[HTTPStatus] = HTTPStatus.INTERNAL_SERVER_ERROR
    default_message: ClassVar[str] = "Internal server error"
    # Extra response headers (e.g. Retry-After). Plain data, so this module stays web-free.
    headers: Mapping[str, str] | None = None

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


class InvalidCurrentPasswordError(AppError):
    """Changing the password with a wrong current password. Not 401: the session is valid."""

    code = ErrorCode.INVALID_CURRENT_PASSWORD
    status_code = HTTPStatus.BAD_REQUEST
    default_message = "The current password is incorrect"


class ResetTokenInvalidError(AppError):
    """Same answer for an unknown, expired or already used reset link."""

    code = ErrorCode.RESET_TOKEN_INVALID
    status_code = HTTPStatus.BAD_REQUEST
    default_message = "The password reset link is invalid or has expired"


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


class SearchNotConfirmedError(AppError):
    """Only confirmed parameters run (SEARCH-004/005)."""

    code = ErrorCode.SEARCH_NOT_CONFIRMED
    status_code = HTTPStatus.CONFLICT
    default_message = "Confirm the search parameters before running the search"


class NoSourcesEnabledError(AppError):
    code = ErrorCode.NO_SOURCES_ENABLED
    status_code = HTTPStatus.CONFLICT
    default_message = "No job source is currently enabled"


class JobLimitReachedError(AppError):
    code = ErrorCode.JOB_LIMIT_REACHED
    status_code = HTTPStatus.CONFLICT
    default_message = "Too many searches are running; wait for one to finish"


class RateLimitedError(AppError):
    code = ErrorCode.RATE_LIMITED
    status_code = HTTPStatus.TOO_MANY_REQUESTS
    default_message = "Too many requests; try again later"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: ErrorDetails = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message, details=details)
        if retry_after_seconds is not None:
            self.headers = {"Retry-After": str(retry_after_seconds)}


class IdempotencyKeyReusedError(AppError):
    code = ErrorCode.IDEMPOTENCY_KEY_REUSED
    status_code = HTTPStatus.CONFLICT
    default_message = "This Idempotency-Key was already used for another search"


class JobAlreadyFinishedError(AppError):
    code = ErrorCode.JOB_ALREADY_FINISHED
    status_code = HTTPStatus.CONFLICT
    default_message = "The search run has already finished"


class JobNotRunningError(AppError):
    """Internal API: the run was cancelled or already finished, so the orchestrator should
    stop working on it (architecture §8.3)."""

    code = ErrorCode.JOB_NOT_RUNNING
    status_code = HTTPStatus.GONE
    default_message = "The search run is not running"


class AIUnavailableError(AppError):
    """The AI can't answer right now (not configured, provider down, timed out, rate-limited,
    circuit open). The UI falls back to the form (architecture §9)."""

    code = ErrorCode.AI_UNAVAILABLE
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    default_message = "The AI assistant is unavailable; use the search form"


class AIInvalidResponseError(AppError):
    """The AI answered twice without matching the expected structure (AI-007)."""

    code = ErrorCode.AI_INVALID_RESPONSE
    status_code = HTTPStatus.BAD_GATEWAY
    default_message = "The AI assistant gave an unusable answer; use the search form"
