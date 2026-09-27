import uuid
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentAuth, DbSessionDep
from app.schemas.errors import ErrorOut
from app.schemas.vacancy import VacancyDetailOut, VacancyListParams, VacancyPageOut
from app.services.vacancy_service import VacancyService

router = APIRouter(prefix="/vacancies", tags=["vacancies"])

UNAUTHENTICATED: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED: {"model": ErrorOut, "description": "No valid session"}
}


def get_vacancy_service(db: DbSessionDep) -> VacancyService:
    return VacancyService(db)


VacancyServiceDep = Annotated[VacancyService, Depends(get_vacancy_service)]


@router.get("", response_model=VacancyPageOut, responses=UNAUTHENTICATED)
async def list_vacancies(
    auth: CurrentAuth,
    params: Annotated[VacancyListParams, Query()],
    service: VacancyServiceDep,
) -> VacancyPageOut:
    """Vacancies visible to the user (from their search results or saved), one per duplicate
    group, filtered and sorted on the server, with cursor pagination."""
    return await service.list_for_user(auth.user.id, params)


@router.get(
    "/{vacancy_id}",
    response_model=VacancyDetailOut,
    responses={
        **UNAUTHENTICATED,
        HTTPStatus.NOT_FOUND: {"model": ErrorOut, "description": "Unknown or not visible"},
    },
)
async def get_vacancy(
    vacancy_id: uuid.UUID, auth: CurrentAuth, service: VacancyServiceDep
) -> VacancyDetailOut:
    return await service.get_for_user(auth.user.id, vacancy_id)
