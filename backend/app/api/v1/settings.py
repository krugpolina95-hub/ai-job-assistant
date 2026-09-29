from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.api.deps import CsrfProtectedAuth, CurrentAuth, DbSessionDep
from app.schemas.errors import ErrorOut
from app.schemas.settings import PersonalizationOut, SettingsOut, SettingsUpdate
from app.services.settings_service import SettingsService

router = APIRouter(prefix="/settings", tags=["settings"])

UNAUTHENTICATED: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED: {"model": ErrorOut, "description": "No valid session"}
}


def get_settings_service(db: DbSessionDep) -> SettingsService:
    return SettingsService(db)


SettingsServiceDep = Annotated[SettingsService, Depends(get_settings_service)]


@router.get("", response_model=SettingsOut, responses=UNAUTHENTICATED)
async def get_settings(auth: CurrentAuth, service: SettingsServiceDep) -> SettingsOut:
    return await service.get(auth.user.id)


@router.patch(
    "",
    response_model=SettingsOut,
    responses={
        **UNAUTHENTICATED,
        HTTPStatus.FORBIDDEN: {"model": ErrorOut, "description": "CSRF check failed"},
    },
)
async def update_settings(
    body: SettingsUpdate, auth: CsrfProtectedAuth, service: SettingsServiceDep
) -> SettingsOut:
    """Partial update: only the fields sent change; `null` is rejected."""
    return await service.update(auth.user.id, body)


@router.get("/personalization", response_model=PersonalizationOut, responses=UNAUTHENTICATED)
async def get_personalization(auth: CurrentAuth, service: SettingsServiceDep) -> PersonalizationOut:
    """What data is held about the user and where it is used (PROFILE-004)."""
    return await service.personalization(auth.user.id)
