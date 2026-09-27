from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.router import API_V1_PREFIX, api_v1_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.redis import create_redis
from app.db.session import create_db_engine, create_session_factory

logger = structlog.stdlib.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    app.state.db_engine = create_db_engine(settings)
    app.state.db_session_factory = create_session_factory(app.state.db_engine)
    app.state.redis = create_redis(settings)
    logger.info("app_started", app_env=settings.app_env)
    try:
        yield
    finally:
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

    register_error_handlers(app)
    # add_middleware prepends: the last one added is the outermost.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(api_v1_router)
    return app
