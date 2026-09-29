"""Search runs for a user: create (idempotency, limits), read, list, cancel, results
(SEARCH-006, TD-12, TD-13, TD-25). Execution happens through a `JobDispatcher`."""

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import (
    FilterMode,
    SearchFilter,
    SearchJobSourceStatus,
    SearchJobStatus,
    SearchStatus,
)
from app.core.errors import (
    IdempotencyKeyReusedError,
    JobAlreadyFinishedError,
    JobLimitReachedError,
    NoSourcesEnabledError,
    NotFoundError,
    RateLimitedError,
    SearchNotConfirmedError,
)
from app.core.pagination import InvalidCursorError, decode_cursor, encode_cursor
from app.integrations.sources.registry import get_source_definition
from app.models import SearchJob, SearchJobSource
from app.repositories.search import SearchJobRepository
from app.repositories.source import SourceRepository
from app.schemas.search import (
    FilterOutcomeOut,
    FilterWarningOut,
    JobProgressOut,
    JobResultOut,
    JobResultPageOut,
    JobSourceOut,
    SearchJobListOut,
    SearchJobOut,
)
from app.schemas.vacancy import VacancyListParams
from app.services import job_lifecycle
from app.services.dispatch import JobDispatcher
from app.services.job_state import is_terminal
from app.services.search_service import SearchService
from app.services.vacancy_service import VacancyService

logger = structlog.stdlib.get_logger(__name__)


class SearchJobService:
    def __init__(self, db: AsyncSession, settings: Settings, dispatcher: JobDispatcher) -> None:
        self._db = db
        self._settings = settings
        self._dispatcher = dispatcher
        self._jobs = SearchJobRepository(db)

    async def run(
        self, user_id: uuid.UUID, search_id: uuid.UUID, idempotency_key: str | None
    ) -> tuple[SearchJobOut, bool]:
        """Starts a run, or returns the one this request already started. Returns
        (run, created). Guarantees (TD-12): a replayed Idempotency-Key returns the same run; a
        search has at most one active run; a user has at most N active runs and M runs per
        hour. Creation is serialized per user by a row lock, so the limits hold under
        concurrent requests; the partial unique index backs "one active run per search"."""
        search = await SearchService(self._db).get_model(user_id, search_id)
        if search.status is not SearchStatus.CONFIRMED:
            raise SearchNotConfirmedError()

        await self._jobs.lock_user(user_id)
        existing = await self._existing_run(user_id, search_id, idempotency_key)
        if existing is not None:
            existing_id = existing.id  # read before the rollback expires the object
            await self._db.rollback()  # releases the user lock
            return await self.get(user_id, existing_id), False

        now = datetime.now(UTC)
        if await self._jobs.count_active(user_id) >= self._settings.search_max_active_jobs_per_user:
            raise JobLimitReachedError()
        hour_ago = now - timedelta(hours=1)
        if await self._jobs.count_created_since(user_id, hour_ago) >= (
            self._settings.search_max_runs_per_hour
        ):
            raise RateLimitedError(details={"limit": "search_runs_per_hour"})
        sources = [
            source
            for source in await SourceRepository(self._db).list_enabled()
            if get_source_definition(source.adapter_key) is not None
        ]
        if not sources:
            raise NoSourcesEnabledError()

        job = self._jobs.add(
            SearchJob(
                search_id=search.id,
                user_id=user_id,
                status=SearchJobStatus.PENDING,
                idempotency_key=idempotency_key,
                request_id=structlog.contextvars.get_contextvars().get("request_id"),
                # Snapshot: later edits of the search don't change a running job (§6.3).
                context={
                    "params": search.normalized_params,
                    "params_schema_version": search.params_schema_version,
                },
                deadline_at=now + timedelta(seconds=self._settings.search_job_deadline_seconds),
                progress={"sources_total": len(sources), "sources_done": 0, "results_so_far": 0},
            )
        )
        try:
            await self._jobs.flush()
        except IntegrityError:
            # Defense in depth: the unique indexes caught a concurrent duplicate that the user
            # lock didn't serialize. The winning run is returned instead.
            await self._db.rollback()
            existing = await self._existing_run(user_id, search_id, idempotency_key)
            if existing is None:
                raise
            existing_id = existing.id
            await self._db.rollback()
            return await self.get(user_id, existing_id), False
        self._db.add_all(
            SearchJobSource(
                search_job_id=job.id, source_id=source.id, status=SearchJobSourceStatus.PENDING
            )
            for source in sources
        )
        await self._db.commit()
        logger.info("search_job_created", job_id=str(job.id), sources=len(sources))

        await self._dispatcher.dispatch(job.id)
        return await self.get(user_id, job.id), True

    async def _existing_run(
        self, user_id: uuid.UUID, search_id: uuid.UUID, idempotency_key: str | None
    ) -> SearchJob | None:
        if idempotency_key is not None:
            replay = await self._jobs.get_by_idempotency_key(user_id, idempotency_key)
            if replay is not None:
                if replay.search_id != search_id:
                    raise IdempotencyKeyReusedError()
                return replay
        return await self._jobs.get_active_for_search(user_id, search_id)

    async def get(self, user_id: uuid.UUID, job_id: uuid.UUID) -> SearchJobOut:
        job = await self._get_model(user_id, job_id)
        job = await job_lifecycle.expire_if_overdue(self._db, job, datetime.now(UTC))
        return await self._to_out(job)

    async def list_for_search(
        self, user_id: uuid.UUID, search_id: uuid.UUID, cursor: str | None, limit: int
    ) -> SearchJobListOut:
        await SearchService(self._db).get_model(user_id, search_id)
        jobs = await self._jobs.list_for_search(
            user_id, search_id, before=_read_cursor(cursor) if cursor else None, limit=limit
        )
        page = jobs[:limit]
        next_cursor = None
        if len(jobs) > limit and page:
            last = page[-1]
            next_cursor = encode_cursor(
                {"created_at": last.created_at.isoformat(), "id": str(last.id)}
            )
        return SearchJobListOut(
            items=[await self._to_out(job) for job in page], next_cursor=next_cursor
        )

    async def cancel(self, user_id: uuid.UUID, job_id: uuid.UUID) -> SearchJobOut:
        job = await self._get_model(user_id, job_id)
        job = await job_lifecycle.expire_if_overdue(self._db, job, datetime.now(UTC))
        if job.status is SearchJobStatus.CANCELLED:
            return await self._to_out(job)  # repeating a cancel is harmless
        if is_terminal(job.status):
            raise JobAlreadyFinishedError(details={"status": job.status.value})
        cancelled = await job_lifecycle.cancel(self._db, job.id, datetime.now(UTC))
        if cancelled is not None and cancelled.status is not SearchJobStatus.CANCELLED:
            raise JobAlreadyFinishedError(details={"status": cancelled.status.value})
        return await self.get(user_id, job_id)

    async def results(
        self, user_id: uuid.UUID, job_id: uuid.UUID, params: VacancyListParams
    ) -> JobResultPageOut:
        job = await self._get_model(user_id, job_id)
        page = await VacancyService(self._db).list_for_user(user_id, params, search_job_id=job.id)
        unverified = await self._jobs.unverified_filters_for(
            job.id, [item.id for item in page.items]
        )
        return JobResultPageOut(
            items=[
                JobResultOut(
                    **item.model_dump(),
                    unverified_filters=[SearchFilter(f) for f in unverified.get(item.id, [])],
                )
                for item in page.items
            ],
            next_cursor=page.next_cursor,
        )

    async def _get_model(self, user_id: uuid.UUID, job_id: uuid.UUID) -> SearchJob:
        job = await self._jobs.get_for_user(user_id, job_id)
        if job is None:
            raise NotFoundError("Search run not found")
        return job

    async def _to_out(self, job: SearchJob) -> SearchJobOut:
        sources = await self._jobs.list_sources(job.id)
        return SearchJobOut(
            id=job.id,
            search_id=job.search_id,
            status=job.status,
            progress=JobProgressOut(
                sources_total=job.progress.get("sources_total", len(sources)),
                sources_done=job.progress.get("sources_done", 0),
                results_so_far=job.progress.get("results_so_far", 0),
            ),
            result_count=job.result_count,
            error_code=job.error_code,
            sources=[_source_out(source) for source in sources],
            filter_warnings=_filter_warnings(sources),
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            deadline_at=job.deadline_at,
        )


def _source_out(row: SearchJobSource) -> JobSourceOut:
    return JobSourceOut(
        key=row.source.adapter_key,
        name=row.source.name,
        status=row.status,
        attempts=row.attempts,
        fetched_count=row.fetched_count,
        stored_count=row.stored_count,
        error_code=row.error_code,
        filters={
            SearchFilter(name): FilterOutcomeOut.model_validate(outcome)
            for name, outcome in row.filter_report.items()
        },
    )


def _filter_warnings(sources: list[SearchJobSource]) -> list[FilterWarningOut]:
    unsupported: dict[SearchFilter, list[str]] = defaultdict(list)
    unverified: dict[SearchFilter, int] = defaultdict(int)
    for row in sources:
        for name, outcome in row.filter_report.items():
            search_filter = SearchFilter(name)
            if outcome.get("mode") == FilterMode.UNSUPPORTED:
                unsupported[search_filter].append(row.source.adapter_key)
            unverified[search_filter] += int(outcome.get("unverified", 0))
    return [
        FilterWarningOut(
            filter=search_filter,
            unsupported_by=unsupported.get(search_filter, []),
            unverified_results=unverified.get(search_filter, 0),
        )
        for search_filter in SearchFilter
        if unsupported.get(search_filter) or unverified.get(search_filter)
    ]


def _read_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    position = decode_cursor(cursor)
    try:
        created_at = datetime.fromisoformat(str(position["created_at"]))
        return created_at, uuid.UUID(str(position["id"]))
    except (KeyError, ValueError) as exc:
        raise InvalidCursorError() from exc
