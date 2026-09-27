from collections.abc import AsyncIterator
from functools import partial
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.redis import ping_redis
from app.db.session import ping_database
from app.services.readiness import ReadinessCheck


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


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
ReadinessChecksDep = Annotated[dict[str, ReadinessCheck], Depends(get_readiness_checks)]
