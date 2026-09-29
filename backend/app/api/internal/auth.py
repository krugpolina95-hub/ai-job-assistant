"""Authentication of n8n → backend calls (TD-06, architecture §8.3).

Every `/internal/v1` request needs the service token (`Authorization: Bearer`) and a fresh HMAC
signature (`X-Timestamp`, `X-Signature`) over time, method, path and body. Without
INTERNAL_API_TOKEN and INTERNAL_HMAC_SECRET configured, every request is refused. The internal
routes are also unreachable from outside: the Next.js rewrite forwards only `/api/v1`, and the
backend publishes no port (TD-05, compose networks).
"""

import time

import structlog
from fastapi import Request

from app.api.deps import SettingsDep
from app.core.errors import UnauthenticatedError
from app.core.security import signature_is_valid, tokens_match

logger = structlog.stdlib.get_logger(__name__)

TIMESTAMP_HEADER = "X-Timestamp"
SIGNATURE_HEADER = "X-Signature"


async def verify_internal_request(request: Request, settings: SettingsDep) -> None:
    token = settings.internal_api_token
    secret = settings.internal_hmac_secret
    if token is None or secret is None:
        logger.warning("internal_api_disabled", path=request.url.path)
        raise UnauthenticatedError()

    scheme, _, presented = (request.headers.get("authorization") or "").partition(" ")
    body = await request.body()  # cached by Starlette: the route still parses it
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    authentic = (
        scheme.lower() == "bearer"
        and tokens_match(token.get_secret_value(), presented)
        and signature_is_valid(
            secret.get_secret_value(),
            request.headers.get(SIGNATURE_HEADER),
            timestamp=request.headers.get(TIMESTAMP_HEADER),
            method=request.method,
            path=path,
            body=body,
            now=time.time(),
            max_age_seconds=settings.internal_signature_max_age_seconds,
        )
    )
    if not authentic:
        logger.warning("internal_request_rejected", path=request.url.path)
        raise UnauthenticatedError()
