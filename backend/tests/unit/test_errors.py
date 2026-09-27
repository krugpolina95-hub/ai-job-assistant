from http import HTTPStatus

from app.core.errors import (
    AppError,
    ConflictError,
    ErrorCode,
    NotFoundError,
    ValidationFailedError,
)


def test_subclasses_fix_code_and_status() -> None:
    assert (NotFoundError.code, NotFoundError.status_code) == (
        ErrorCode.NOT_FOUND,
        HTTPStatus.NOT_FOUND,
    )
    assert (ConflictError.code, ConflictError.status_code) == (
        ErrorCode.CONFLICT,
        HTTPStatus.CONFLICT,
    )
    assert (ValidationFailedError.code, ValidationFailedError.status_code) == (
        ErrorCode.VALIDATION_ERROR,
        HTTPStatus.UNPROCESSABLE_ENTITY,
    )


def test_default_and_custom_message_and_details() -> None:
    assert NotFoundError().message == "Resource not found"
    assert NotFoundError().details is None

    error = ConflictError("Already running", details={"job_id": "x"})

    assert error.message == "Already running"
    assert error.details == {"job_id": "x"}
    assert str(error) == "Already running"


def test_all_domain_errors_share_a_base() -> None:
    assert all(
        issubclass(cls, AppError) for cls in (NotFoundError, ConflictError, ValidationFailedError)
    )
