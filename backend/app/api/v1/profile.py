from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import CsrfProtectedAuth, CurrentAuth, DbSessionDep
from app.schemas.errors import ErrorOut
from app.schemas.profile import (
    ProfileOut,
    ProfileUpdate,
    SkillListOut,
    SkillSearchParams,
    UserSkillsIn,
    UserSkillsOut,
)
from app.services.profile_service import ProfileService

router = APIRouter(tags=["profile"])

UNAUTHENTICATED: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED: {"model": ErrorOut, "description": "No valid session"}
}
UNSAFE: dict[int | str, dict[str, Any]] = {
    **UNAUTHENTICATED,
    HTTPStatus.FORBIDDEN: {"model": ErrorOut, "description": "CSRF check failed"},
}


def get_profile_service(db: DbSessionDep) -> ProfileService:
    return ProfileService(db)


ProfileServiceDep = Annotated[ProfileService, Depends(get_profile_service)]


@router.get("/profile", response_model=ProfileOut, responses=UNAUTHENTICATED)
async def get_profile(auth: CurrentAuth, service: ProfileServiceDep) -> ProfileOut:
    """The signed-in user's profile. Empty (`updated_at: null`) until first saved."""
    return await service.get(auth.user.id)


@router.patch("/profile", response_model=ProfileOut, responses=UNSAFE)
async def update_profile(
    body: ProfileUpdate, auth: CsrfProtectedAuth, service: ProfileServiceDep
) -> ProfileOut:
    """Partial update: only the fields sent change; `null` clears a nullable field."""
    return await service.update(auth.user.id, body)


@router.delete("/profile", status_code=HTTPStatus.NO_CONTENT, responses=UNSAFE)
async def clear_profile(auth: CsrfProtectedAuth, service: ProfileServiceDep) -> None:
    """Deletes all profile data and skills. The account and its settings stay."""
    await service.clear(auth.user.id)


@router.get("/profile/skills", response_model=UserSkillsOut, responses=UNAUTHENTICATED)
async def get_profile_skills(auth: CurrentAuth, service: ProfileServiceDep) -> UserSkillsOut:
    return await service.get_skills(auth.user.id)


@router.put("/profile/skills", response_model=UserSkillsOut, responses=UNSAFE)
async def replace_profile_skills(
    body: UserSkillsIn, auth: CsrfProtectedAuth, service: ProfileServiceDep
) -> UserSkillsOut:
    """Replaces the whole list. Each name must be a dictionary skill or alias (see
    `GET /skills`); otherwise `422` with the rejected rows in `details`."""
    return await service.replace_skills(auth.user.id, body)


@router.get("/skills", response_model=SkillListOut, responses=UNAUTHENTICATED)
async def search_skills(
    auth: CurrentAuth,
    params: Annotated[SkillSearchParams, Query()],
    service: ProfileServiceDep,
) -> SkillListOut:
    """Skill dictionary lookup for autocomplete, by the start of a name or alias (EN/RU)."""
    return await service.search_skills(params)
