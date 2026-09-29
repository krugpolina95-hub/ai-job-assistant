import uuid
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Response

from app.api.deps import CsrfProtectedAuth, CurrentAuth, DbSessionDep, SearchJobServiceDep
from app.schemas.errors import ErrorOut
from app.schemas.search import SearchIn, SearchJobListOut, SearchJobOut, SearchOut
from app.services.search_service import SearchService

router = APIRouter(prefix="/searches", tags=["searches"])

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    HTTPStatus.UNAUTHORIZED: {"model": ErrorOut, "description": "No valid session"},
    HTTPStatus.NOT_FOUND: {"model": ErrorOut, "description": "Unknown or another user's search"},
}
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
        description="Unique per click; a replay returns the same run (TD-12).",
    ),
]


@router.post("", status_code=HTTPStatus.CREATED, response_model=SearchOut, responses=NOT_FOUND)
async def create_search(body: SearchIn, auth: CsrfProtectedAuth, db: DbSessionDep) -> SearchOut:
    return await SearchService(db).create(auth.user.id, body)


@router.get("/{search_id}", response_model=SearchOut, responses=NOT_FOUND)
async def get_search(search_id: uuid.UUID, auth: CurrentAuth, db: DbSessionDep) -> SearchOut:
    return await SearchService(db).get(auth.user.id, search_id)


@router.post(
    "/{search_id}/run",
    status_code=HTTPStatus.ACCEPTED,
    response_model=SearchJobOut,
    responses={
        **NOT_FOUND,
        HTTPStatus.OK: {"model": SearchJobOut, "description": "Replay or already running"},
        HTTPStatus.CONFLICT: {
            "model": ErrorOut,
            "description": "JOB_LIMIT_REACHED, NO_SOURCES_ENABLED, SEARCH_NOT_CONFIRMED, "
            "IDEMPOTENCY_KEY_REUSED",
        },
        HTTPStatus.TOO_MANY_REQUESTS: {"model": ErrorOut, "description": "Too many runs"},
    },
)
async def run_search(
    search_id: uuid.UUID,
    auth: CsrfProtectedAuth,
    service: SearchJobServiceDep,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> SearchJobOut:
    """Starts an asynchronous run (SEARCH-006) and returns immediately; poll the run."""
    job, created = await service.run(auth.user.id, search_id, idempotency_key)
    if not created:
        response.status_code = HTTPStatus.OK
    return job


@router.get("/{search_id}/jobs", response_model=SearchJobListOut, responses=NOT_FOUND)
async def list_search_jobs(
    search_id: uuid.UUID,
    auth: CurrentAuth,
    service: SearchJobServiceDep,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SearchJobListOut:
    return await service.list_for_search(auth.user.id, search_id, cursor, limit)
