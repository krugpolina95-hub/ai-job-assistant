"""`/internal/v1`: the only door n8n has into the backend (TD-06, architecture §8.3).

Every route is authenticated by `verify_internal_request`, attached here once so no route can
forget it. The routes are left out of the public OpenAPI document (and so out of the generated
frontend types). Each maps one runner step: the business rules stay in the services.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from app.api.internal.auth import verify_internal_request
from app.schemas.internal import (
    FinalizeOut,
    JobContextOut,
    JobStatusIn,
    JobStatusOut,
    SourceRunOut,
    SweepOut,
)
from app.services.job_runner import SearchJobRunner

INTERNAL_V1_PREFIX = "/internal/v1"

internal_v1_router = APIRouter(
    prefix=INTERNAL_V1_PREFIX,
    dependencies=[Depends(verify_internal_request)],
    include_in_schema=False,
)


def get_job_runner(request: Request) -> SearchJobRunner:
    runner: SearchJobRunner = request.app.state.job_runner
    return runner


JobRunnerDep = Annotated[SearchJobRunner, Depends(get_job_runner)]
SourceKey = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")]


@internal_v1_router.get("/jobs/{job_id}/context", response_model=JobContextOut)
async def job_context(job_id: uuid.UUID, runner: JobRunnerDep) -> JobContextOut:
    return await runner.context(job_id)


@internal_v1_router.post("/jobs/{job_id}/status", response_model=JobStatusOut)
async def job_status(job_id: uuid.UUID, body: JobStatusIn, runner: JobRunnerDep) -> JobStatusOut:
    """`running` (idempotent). `410 JOB_NOT_RUNNING` when the run is already over."""
    return await runner.mark_running(job_id)


@internal_v1_router.post("/jobs/{job_id}/sources/{source_key}/run", response_model=SourceRunOut)
async def run_source(
    job_id: uuid.UUID, source_key: SourceKey, runner: JobRunnerDep
) -> SourceRunOut:
    """Runs one source (adapter → ingest → link). Safe to repeat: a finished source returns its
    recorded outcome. `410 JOB_NOT_RUNNING` when the run is over: stop."""
    return await runner.run_source_by_key(job_id, source_key)


@internal_v1_router.post("/jobs/{job_id}/finalize", response_model=FinalizeOut)
async def finalize(job_id: uuid.UUID, runner: JobRunnerDep) -> FinalizeOut:
    return await runner.finalize(job_id)


@internal_v1_router.post("/maintenance/sweep-jobs", response_model=SweepOut)
async def sweep_jobs(runner: JobRunnerDep) -> SweepOut:
    return SweepOut(finalized=await runner.sweep())
