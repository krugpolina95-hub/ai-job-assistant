from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.internal.router import internal_v1_router
from app.api.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.router import API_V1_PREFIX, api_v1_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.rate_limit import RedisRateLimiter
from app.core.redis import create_redis
from app.core.security import PasswordHasher
from app.db.session import create_db_engine, create_session_factory
from app.integrations.email.background import BackgroundEmailSender
from app.integrations.email.smtp import SmtpEmailSender
from app.integrations.n8n.webhook import N8nWebhookClient
from app.integrations.sources.rate_limit import RedisSourceRateLimiter
from app.services.dispatch import InlineDispatcher, N8nDispatcher
from app.services.job_runner import SearchJobRunner

logger = structlog.stdlib.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    app.state.db_engine = create_db_engine(settings)
    app.state.db_session_factory = create_session_factory(app.state.db_engine)
    app.state.redis = create_redis(settings)
    app.state.auth_rate_limiter = RedisRateLimiter(
        app.state.redis, key_secret=settings.log_hash_secret.get_secret_value()
    )
    email_sender = BackgroundEmailSender(SmtpEmailSender(settings))
    app.state.email_sender = email_sender
    runner = SearchJobRunner(
        app.state.db_session_factory, settings, RedisSourceRateLimiter(app.state.redis)
    )
    app.state.job_runner = runner
    n8n_http: httpx.AsyncClient | None = None
    dispatcher: InlineDispatcher | N8nDispatcher
    if settings.job_dispatcher == "n8n":
        if settings.n8n_webhook_url is None or settings.n8n_webhook_secret is None:
            raise RuntimeError("n8n dispatcher without webhook settings")  # config validates
        n8n_http = httpx.AsyncClient(
            timeout=settings.n8n_webhook_timeout_seconds, follow_redirects=False
        )
        webhook = N8nWebhookClient(
            n8n_http,
            url=str(settings.n8n_webhook_url),
            secret=settings.n8n_webhook_secret.get_secret_value(),
        )
        dispatcher = N8nDispatcher(webhook, app.state.db_session_factory)
    else:
        dispatcher = InlineDispatcher(runner)
    app.state.dispatcher = dispatcher
    logger.info("app_started", app_env=settings.app_env, job_dispatcher=settings.job_dispatcher)
    try:
        yield
    finally:
        if isinstance(dispatcher, InlineDispatcher):
            await dispatcher.drain()
        if n8n_http is not None:
            await n8n_http.aclose()
        await email_sender.drain()
        await app.state.redis.aclose()
        await app.state.db_engine.dispose()
        logger.info("app_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title="AI Job Assistant API",
        version="0.1.0",
        lifespan=lifespan,
        openapi_url=f"{API_V1_PREFIX}/openapi.json" if settings.expose_api_docs else None,
        docs_url=f"{API_V1_PREFIX}/docs" if settings.expose_api_docs else None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.password_hasher = PasswordHasher(
        time_cost=settings.password_hash_time_cost,
        memory_kib=settings.password_hash_memory_kib,
        parallelism=settings.password_hash_parallelism,
    )

    register_error_handlers(app)
    # add_middleware prepends: the last one added is the outermost.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(api_v1_router)
    app.include_router(internal_v1_router)
    return app
