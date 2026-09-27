"""Pure ASGI middlewares (no BaseHTTPMiddleware: it breaks contextvars and streaming).

Order, outermost first (see `app/main.py`):
    SecurityHeaders → RequestContext → BodySizeLimit → routing → exception handlers
Security headers are outermost so they apply to every response, including the last-resort 500.
"""

import re
import time
from http import HTTPStatus

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import error_response
from app.core.errors import ErrorCode
from app.core.ids import uuid7

logger = structlog.stdlib.get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def resolve_request_id(incoming: str | None) -> str:
    """Reuse the ID from the proxy/frontend when it is well-formed, so logs correlate across
    services (TD-21). Anything else is replaced, never logged verbatim."""
    if incoming and _VALID_REQUEST_ID.fullmatch(incoming):
        return incoming
    return uuid7().hex


def _request_id(scope: Scope) -> str | None:
    request_id: str | None = scope.get("state", {}).get("request_id")
    return request_id


class RequestContextMiddleware:
    """Request ID, access log and last-resort handling of unexpected exceptions."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = resolve_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        scope.setdefault("state", {})["request_id"] = request_id
        started_at = time.perf_counter()
        status_code = HTTPStatus.INTERNAL_SERVER_ERROR.value
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            except Exception:
                logger.exception("unhandled_exception")
                if response_started:
                    raise
                response = error_response(
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                    code=ErrorCode.INTERNAL_ERROR,
                    message="Internal server error",
                    request_id=request_id,
                )
                await response(scope, receive, send_with_request_id)
            finally:
                # Path only: query strings may carry personal data.
                logger.info(
                    "http_request",
                    method=scope["method"],
                    path=scope["path"],
                    status=status_code,
                    duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
                )


class BodySizeLimitMiddleware:
    """Rejects request bodies above the cap (64 KB by default, CLAUDE.md §7) with 413."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            rejection = self._check_declared_length(declared)
            if rejection is not None:
                response = error_response(
                    status_code=rejection,
                    code=(
                        ErrorCode.PAYLOAD_TOO_LARGE
                        if rejection == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                        else ErrorCode.BAD_REQUEST
                    ),
                    message=rejection.phrase,
                    request_id=_request_id(scope),
                )
                await response(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            # Covers chunked bodies and lying Content-Length headers.
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return message

        await self.app(scope, limited_receive, send)

    def _check_declared_length(self, declared: str) -> HTTPStatus | None:
        if not declared.isdigit():
            return HTTPStatus.BAD_REQUEST
        if int(declared) > self.max_bytes:
            return HTTPStatus.REQUEST_ENTITY_TOO_LARGE
        return None


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
