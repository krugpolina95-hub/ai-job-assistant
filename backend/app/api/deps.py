from functools import partial
from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings
from app.core.redis import ping_redis
from app.db.session import ping_database
from app.services.readiness import ReadinessCheck


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_readiness_checks(request: Request) -> dict[str, ReadinessCheck]:
    return {
        "database": partial(ping_database, request.app.state.db_engine),
        "redis": partial(ping_redis, request.app.state.redis),
    }


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
ReadinessChecksDep = Annotated[dict[str, ReadinessCheck], Depends(get_readiness_checks)]
