"""Maps every error to the response envelope (SRS §6, TD-19):

{"error": {"code": ..., "message": ..., "details": ..., "request_id": ...}}
"""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, ErrorCode, ErrorDetails
from app.schemas.errors import ErrorBodyOut, ErrorOut, FieldErrorOut

_HTTP_STATUS_CODES: dict[int, ErrorCode] = {
    HTTPStatus.BAD_REQUEST: ErrorCode.BAD_REQUEST,
    HTTPStatus.NOT_FOUND: ErrorCode.NOT_FOUND,
    HTTPStatus.METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
    HTTPStatus.CONFLICT: ErrorCode.CONFLICT,
    HTTPStatus.REQUEST_ENTITY_TOO_LARGE: ErrorCode.PAYLOAD_TOO_LARGE,
    HTTPStatus.UNPROCESSABLE_ENTITY: ErrorCode.VALIDATION_ERROR,
}


def error_response(
    *,
    status_code: int,
    code: ErrorCode,
    message: str,
    request_id: str | None,
    details: ErrorDetails | list[FieldErrorOut] = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ErrorOut(
        error=ErrorBodyOut.model_validate(
            {"code": code, "message": message, "details": details, "request_id": request_id}
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=dict(headers) if headers else None,
    )


def request_id_of(request: Request) -> str | None:
    request_id: str | None = getattr(request.state, "request_id", None)
    return request_id


async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    return error_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        request_id=request_id_of(request),
        headers=exc.headers,
    )


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Only location, type and message: `input` and `ctx` could echo passwords or other PII.
    details = [
        FieldErrorOut(loc=list(error["loc"]), type=error["type"], message=error["msg"])
        for error in exc.errors()
    ]
    return error_response(
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        code=ErrorCode.VALIDATION_ERROR,
        message="Request validation failed",
        details=details,
        request_id=request_id_of(request),
    )


async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    status = HTTPStatus(exc.status_code)
    code = _HTTP_STATUS_CODES.get(
        status, ErrorCode.BAD_REQUEST if status < 500 else ErrorCode.INTERNAL_ERROR
    )
    return error_response(
        status_code=status,
        code=code,
        message=status.phrase,
        request_id=request_id_of(request),
        headers=exc.headers,
    )


def register_error_handlers(app: FastAPI) -> None:
    app.exception_handler(AppError)(handle_app_error)
    app.exception_handler(RequestValidationError)(handle_validation_error)
    app.exception_handler(StarletteHTTPException)(handle_http_exception)


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNPROCESSABLE_ENTITY: {"model": ErrorOut, "description": "Validation error"},
    HTTPStatus.INTERNAL_SERVER_ERROR: {"model": ErrorOut, "description": "Internal error"},
}
