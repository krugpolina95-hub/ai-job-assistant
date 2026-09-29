import hashlib
import uuid
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Response

from app.api.deps import CsrfProtectedAuth, CurrentAuth, SearchJobServiceDep
from app.schemas.errors import ErrorOut
from app.schemas.search import JobResultPageOut, SearchJobOut
from app.schemas.vacancy import VacancyListParams

router = APIRouter(prefix="/search-jobs", tags=["search runs"])

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED: {"model": ErrorOut, "description": "No valid session"},
    HTTPStatus.NOT_FOUND: {"model": ErrorOut, "description": "Unknown or another user's run"},
}


@router.get(
    "/{job_id}",
    response_model=SearchJobOut,
    responses={**NOT_FOUND, HTTPStatus.NOT_MODIFIED: {"description": "Unchanged since ETag"}},
)
async def get_search_job(
    job_id: uuid.UUID,
    auth: CurrentAuth,
    service: SearchJobServiceDep,
    response: Response,
    if_none_match: Annotated[str | None, Header(max_length=200)] = None,
) -> Any:
    """Status, progress, per-source outcomes and filter warnings. Poll every 2-5 s until the
    status is terminal (TD-25); send `If-None-Match` to get `304` while nothing changed."""
    job = await service.get(auth.user.id, job_id)
    etag = 'W/"' + hashlib.sha256(job.model_dump_json().encode()).hexdigest()[:32] + '"'
    if if_none_match == etag:
        return Response(status_code=HTTPStatus.NOT_MODIFIED, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return job


@router.post(
    "/{job_id}/cancel",
    response_model=SearchJobOut,
    responses={
        **NOT_FOUND,
        HTTPStatus.CONFLICT: {"model": ErrorOut, "description": "JOB_ALREADY_FINISHED"},
    },
)
async def cancel_search_job(
    job_id: uuid.UUID, auth: CsrfProtectedAuth, service: SearchJobServiceDep
) -> SearchJobOut:
    return await service.cancel(auth.user.id, job_id)


@router.get("/{job_id}/results", response_model=JobResultPageOut, responses=NOT_FOUND)
async def search_job_results(
    job_id: uuid.UUID,
    auth: CurrentAuth,
    params: Annotated[VacancyListParams, Query()],
    service: SearchJobServiceDep,
) -> JobResultPageOut:
    """The run's vacancies with the same filters, sorting, cursor and duplicate grouping as
    `/vacancies`. `unverified_filters` lists requested filters a result couldn't be checked
    against."""
    return await service.results(auth.user.id, job_id, params)
