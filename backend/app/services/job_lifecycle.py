"""Job state changes shared by the runner (background) and request handlers (lazy deadline,
cancel). Each function locks the job row, so concurrent callers see a consistent state, and
each is idempotent: calling it again changes nothing."""

import uuid
from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import SearchJobSourceStatus, SearchJobStatus
from app.integrations.sources.base import SourceErrorCode
from app.models import SearchJob, SearchJobSource
from app.repositories.search import SearchJobRepository
from app.services.job_state import FINISHED_SOURCE_STATUSES, final_status, is_terminal

logger = structlog.stdlib.get_logger(__name__)


async def refresh_progress(repo: SearchJobRepository, job: SearchJob) -> None:
    await repo.flush()  # count what was changed in this transaction, not the committed state
    sources = await repo.list_sources(job.id)
    job.progress = {
        "sources_total": len(sources),
        "sources_done": sum(s.status in FINISHED_SOURCE_STATUSES for s in sources),
        "results_so_far": await repo.count_results(job.id),
    }


async def finalize(db: AsyncSession, job_id: uuid.UUID, now: datetime) -> SearchJob | None:
    """Sets the terminal status once every source has finished, or at the deadline with
    whatever finished by then (unfinished sources become timed_out). Commits."""
    repo = SearchJobRepository(db)
    job = await repo.lock_for_runner(job_id)
    if job is None or is_terminal(job.status):
        return job
    sources = await repo.list_sources(job.id)
    unfinished = [s for s in sources if s.status not in FINISHED_SOURCE_STATUSES]
    if unfinished and now < job.deadline_at:
        return job  # still running within its time
    _close(unfinished, SearchJobSourceStatus.TIMED_OUT, SourceErrorCode.DEADLINE_EXCEEDED, now)

    status, error_code = final_status(s.status for s in sources)
    job.status = status
    job.error_code = error_code
    job.finished_at = now
    job.started_at = job.started_at or now
    job.result_count = await repo.count_results(job.id)
    await refresh_progress(repo, job)
    await db.commit()
    logger.info(
        "search_job_finished",
        job_id=str(job.id),
        status=status,
        error_code=error_code,
        results=job.result_count,
    )
    return job


async def expire_if_overdue(db: AsyncSession, job: SearchJob, now: datetime) -> SearchJob:
    """Lazy sweep (NFR-07): a run past its deadline is finalized when someone looks at it,
    even if its worker died."""
    if is_terminal(job.status) or now < job.deadline_at:
        return job
    finalized = await finalize(db, job.id, now)
    return finalized or job


async def cancel(db: AsyncSession, job_id: uuid.UUID, now: datetime) -> SearchJob | None:
    """Cooperative cancel (TD-12, GAP-22): the run ends now; sources still in flight finish
    their import, but their results are not linked to the run."""
    repo = SearchJobRepository(db)
    job = await repo.lock_for_runner(job_id)
    if job is None or is_terminal(job.status):
        return job
    job.status = SearchJobStatus.CANCELLED
    job.cancel_requested_at = now
    job.finished_at = now
    sources = await repo.list_sources(job.id)
    _close(
        [s for s in sources if s.status not in FINISHED_SOURCE_STATUSES],
        SearchJobSourceStatus.SKIPPED,
        SourceErrorCode.CANCELLED,
        now,
    )
    job.result_count = await repo.count_results(job.id)
    await refresh_progress(repo, job)
    await db.commit()
    logger.info("search_job_cancelled", job_id=str(job.id))
    return job


def _close(
    sources: list[SearchJobSource],
    status: SearchJobSourceStatus,
    error_code: SourceErrorCode,
    now: datetime,
) -> None:
    for source in sources:
        source.status = status
        source.error_code = error_code
        source.finished_at = now
