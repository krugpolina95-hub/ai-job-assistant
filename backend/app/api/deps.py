from collections.abc import AsyncIterator
from functools import partial
from typing import Annotated

import structlog
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.cookies import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from app.core.config import Settings
from app.core.errors import CsrfFailedError
from app.core.redis import ping_redis
from app.core.security import PasswordHasher, hash_identifier, tokens_match
from app.db.session import ping_database
from app.services.auth_service import AuthContext, AuthService
from app.services.dispatch import JobDispatcher
from app.services.readiness import ReadinessCheck
from app.services.search_job_service import SearchJobService


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request. Services own the transaction and commit; anything
    left uncommitted is rolled back when the session closes."""
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.db_session_factory
    async with session_factory() as session:
        yield session


def get_readiness_checks(request: Request) -> dict[str, ReadinessCheck]:
    return {
        "database": partial(ping_database, request.app.state.db_engine),
        "redis": partial(ping_redis, request.app.state.redis),
    }


def get_password_hasher(request: Request) -> PasswordHasher:
    hasher: PasswordHasher = request.app.state.password_hasher
    return hasher


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
ReadinessChecksDep = Annotated[dict[str, ReadinessCheck], Depends(get_readiness_checks)]


def get_auth_service(
    db: DbSessionDep,
    settings: SettingsDep,
    hasher: Annotated[PasswordHasher, Depends(get_password_hasher)],
) -> AuthService:
    return AuthService(db, hasher, settings)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


async def get_current_auth(
    request: Request, service: AuthServiceDep, settings: SettingsDep
) -> AuthContext:
    """401 unless the `sid` cookie maps to a live session. Use for every protected route."""
    auth = await service.authenticate(request.cookies.get(SESSION_COOKIE))
    structlog.contextvars.bind_contextvars(
        user=hash_identifier(str(auth.user.id), settings.log_hash_secret.get_secret_value())
    )
    return auth


CurrentAuth = Annotated[AuthContext, Depends(get_current_auth)]


async def get_current_auth_with_csrf(request: Request, auth: CurrentAuth) -> AuthContext:
    """For unsafe methods (POST/PUT/PATCH/DELETE): authentication first (401), then the
    double-submit check (403). tests/authz enforces that every unsafe protected route uses it."""
    if not tokens_match(request.cookies.get(CSRF_COOKIE), request.headers.get(CSRF_HEADER)):
        raise CsrfFailedError()
    return auth


CsrfProtectedAuth = Annotated[AuthContext, Depends(get_current_auth_with_csrf)]


def get_dispatcher(request: Request) -> JobDispatcher:
    dispatcher: JobDispatcher = request.app.state.dispatcher
    return dispatcher


def get_search_job_service(
    db: DbSessionDep,
    settings: SettingsDep,
    dispatcher: Annotated[JobDispatcher, Depends(get_dispatcher)],
) -> SearchJobService:
    return SearchJobService(db, settings, dispatcher)


SearchJobServiceDep = Annotated[SearchJobService, Depends(get_search_job_service)]
