"""Executes one search run (M5.4/M5.8/M5.9): the steps a dispatcher drives.

    start → every source in parallel (claim → SourceRunService → filter → link) → finalize

Each step is idempotent and safe with several workers or processes: a source is claimed
atomically, outcomes are fenced by attempt number, links never duplicate, and state changes
lock the job row. The same steps back the /internal/v1 endpoints n8n calls (TD-06, TD-09):
nothing here knows who calls it.
"""

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.enums import FilterMode, SearchFilter, SearchJobSourceStatus, SearchJobStatus
from app.core.errors import JobNotRunningError, NotFoundError, SourceNotEnabledError
from app.integrations.sources.base import RETRYABLE_SOURCE_ERRORS, SourceErrorCode
from app.integrations.sources.rate_limit import SourceRateLimiter
from app.integrations.sources.registry import get_source_definition
from app.models import Vacancy
from app.repositories.search import SearchJobRepository
from app.schemas.internal import FinalizeOut, JobContextOut, JobStatusOut, SourceRunOut
from app.schemas.search import SearchParamsV1
from app.services import job_lifecycle, search_filters
from app.services.job_state import is_terminal
from app.services.source_run_service import SourceRunResult, SourceRunService

logger = structlog.stdlib.get_logger(__name__)


class _Attempt(Enum):
    DONE = "done"
    RETRY = "retry"


@dataclass(frozen=True)
class _Claim:
    attempt: int
    source_key: str
    source_enabled: bool
    params: SearchParamsV1
    deadline_at: datetime


class SearchJobRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        limiter: SourceRateLimiter,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = session_factory
        self._settings = settings
        self._limiter = limiter
        self._now = clock

    async def run(self, job_id: uuid.UUID) -> None:
        """The whole run in this process (InlineDispatcher). n8n drives the same steps one by
        one through /internal/v1 (TD-09)."""
        if not await self.start(job_id):
            return
        async with self._sessions() as db:
            sources = await SearchJobRepository(db).list_sources(job_id)
            source_ids = [source.source_id for source in sources]
        # One failing source never stops the others: each runs in its own task and session.
        await asyncio.gather(*(self.run_source(job_id, source_id) for source_id in source_ids))
        await self.finalize(job_id)

    # --- steps for the orchestrator (n8n) -------------------------------------------------

    async def context(self, job_id: uuid.UUID) -> JobContextOut:
        async with self._sessions() as db:
            repo = SearchJobRepository(db)
            job = await repo.get_by_id_for_runner(job_id)
            if job is None:
                raise NotFoundError()
            sources = await repo.list_sources(job_id)
            return JobContextOut(
                job_id=job.id,
                status=job.status,
                deadline_at=job.deadline_at,
                sources=[source.source.adapter_key for source in sources],
            )

    async def mark_running(self, job_id: uuid.UUID) -> JobStatusOut:
        """`start` for the orchestrator: 404 for an unknown run, 410 when it is over."""
        if not await self.start(job_id):
            status = (await self.context(job_id)).status
            raise JobNotRunningError(details={"status": status})
        return JobStatusOut(job_id=job_id, status=SearchJobStatus.RUNNING)

    async def run_source_by_key(self, job_id: uuid.UUID, key: str) -> SourceRunOut:
        """Runs one source of a run (WF-002) and reports its recorded outcome. Idempotent: a
        source already done is not run again, its outcome is returned as is."""
        async with self._sessions() as db:
            repo = SearchJobRepository(db)
            job = await repo.get_by_id_for_runner(job_id)
            source = next(
                (s for s in await repo.list_sources(job_id) if s.source.adapter_key == key), None
            )
            if job is None or source is None:
                raise NotFoundError()
            status, source_id = job.status, source.source_id
        if is_terminal(status):
            raise JobNotRunningError(details={"status": status})
        if status is SearchJobStatus.PENDING:
            await self.start(job_id)  # the orchestrator skipped the status call: harmless
        await self.run_source(job_id, source_id)
        async with self._sessions() as db:
            row = await SearchJobRepository(db).get_source_run(job_id, source_id)
            if row is None:
                raise NotFoundError()
            return SourceRunOut(
                job_id=job_id,
                source=key,
                status=row.status,
                attempts=row.attempts,
                fetched_count=row.fetched_count,
                stored_count=row.stored_count,
                error_code=row.error_code,
            )

    async def finalize(self, job_id: uuid.UUID) -> FinalizeOut:
        """Terminal status once every source has finished or the deadline passed; before that
        the run stays `running` and the call can be repeated (or the sweep finishes it)."""
        async with self._sessions() as db:
            job = await job_lifecycle.finalize(db, job_id, self._now())
            if job is None:
                raise NotFoundError()
            return FinalizeOut(
                job_id=job.id,
                status=job.status,
                result_count=job.result_count,
                error_code=job.error_code,
            )

    async def sweep(self, limit: int = 100) -> int:
        """Finalizes runs past their deadline whose orchestrator never finished them
        (n8n Cron, NFR-07). Reads also do this lazily; the sweep covers runs nobody reads."""
        async with self._sessions() as db:
            overdue = await SearchJobRepository(db).list_overdue_ids(self._now(), limit)
        finalized = 0
        for job_id in overdue:
            async with self._sessions() as db:
                job = await job_lifecycle.finalize(db, job_id, self._now())
                finalized += bool(job is not None and is_terminal(job.status))
        if finalized:
            logger.info("search_jobs_swept", finalized=finalized)
        return finalized

    async def start(self, job_id: uuid.UUID) -> bool:
        """pending → running. Returns whether the job is running (repeating is harmless)."""
        async with self._sessions() as db:
            repo = SearchJobRepository(db)
            job = await repo.lock_for_runner(job_id)
            if job is None:
                return False
            if job.status is SearchJobStatus.PENDING:
                job.status = SearchJobStatus.RUNNING
                job.started_at = self._now()
                await job_lifecycle.refresh_progress(repo, job)
                await db.commit()
                logger.info("search_job_started", job_id=str(job_id))
            return job.status is SearchJobStatus.RUNNING

    async def run_source(self, job_id: uuid.UUID, source_id: uuid.UUID) -> None:
        """Runs one source for a job, retrying retryable failures while attempts and time
        remain. Safe to call concurrently or repeatedly: only one caller gets each claim."""
        for _ in range(self._settings.source_run_max_attempts):
            if await self._attempt(job_id, source_id) is _Attempt.DONE:
                return
            await asyncio.sleep(self._settings.source_retry_delay_seconds)

    async def _attempt(self, job_id: uuid.UUID, source_id: uuid.UUID) -> _Attempt:
        claim = await self._claim(job_id, source_id)
        if claim is None:
            return _Attempt.DONE

        definition = get_source_definition(claim.source_key)
        if definition is None or not claim.source_enabled:
            # Disabled (or unregistered) after the run was created: nothing to ask it.
            await self._record(job_id, source_id, claim, None, SearchJobSourceStatus.SKIPPED)
            return _Attempt.DONE

        result: SourceRunResult
        try:
            async with self._sessions() as db:
                service = SourceRunService(db, self._settings, self._limiter)
                result = await service.run(
                    claim.source_key,
                    search_filters.source_params(claim.params),
                    deadline=claim.deadline_at,
                )
        except SourceNotEnabledError:
            await self._record(job_id, source_id, claim, None, SearchJobSourceStatus.SKIPPED)
            return _Attempt.DONE
        except Exception:
            # A bug, not a source outage: final for this run, details only in the log.
            logger.exception("source_run_crashed", job_id=str(job_id), source=claim.source_key)
            result = SourceRunResult(
                source=claim.source_key,
                status=SearchJobSourceStatus.FAILED,
                error_code=SourceErrorCode.UNEXPECTED,
            )

        await self._record(job_id, source_id, claim, result, result.status)
        retry = (
            result.status is not SearchJobSourceStatus.SUCCEEDED
            and result.retryable
            and claim.attempt < self._settings.source_run_max_attempts
            and self._now() + timedelta(seconds=self._settings.source_retry_delay_seconds)
            < claim.deadline_at
        )
        return _Attempt.RETRY if retry else _Attempt.DONE

    async def _claim(self, job_id: uuid.UUID, source_id: uuid.UUID) -> _Claim | None:
        now = self._now()
        async with self._sessions() as db:
            repo = SearchJobRepository(db)
            attempt = await repo.claim_source(
                job_id,
                source_id,
                now=now,
                max_attempts=self._settings.source_run_max_attempts,
                retryable_codes=[code.value for code in RETRYABLE_SOURCE_ERRORS],
                stale_before=now - timedelta(seconds=self._settings.source_run_lease_seconds),
            )
            if attempt is None:
                await db.rollback()
                return None
            await db.commit()
            job = await repo.get_by_id_for_runner(job_id)
            row = await repo.get_source_run(job_id, source_id)
            if job is None or row is None:
                return None
            source = row.source
            return _Claim(
                attempt=attempt,
                source_key=source.adapter_key,
                source_enabled=source.is_enabled,
                params=SearchParamsV1.model_validate(job.context["params"]),
                deadline_at=job.deadline_at,
            )

    async def _record(
        self,
        job_id: uuid.UUID,
        source_id: uuid.UUID,
        claim: _Claim,
        result: SourceRunResult | None,
        status: SearchJobSourceStatus,
    ) -> None:
        """Stores the source outcome and links matching results, in one transaction with the
        job row locked. Results are discarded when the run was cancelled or already finished,
        or when this worker's claim was taken over (fencing)."""
        now = self._now()
        definition = get_source_definition(claim.source_key)
        modes = search_filters.filter_modes(
            claim.params, definition.filter_support if definition else {}
        )
        async with self._sessions() as db:
            repo = SearchJobRepository(db)
            job = await repo.lock_for_runner(job_id)
            if job is None:
                return
            links: list[tuple[uuid.UUID, list[str]]] = []
            report = {f: {"mode": m, "excluded": 0, "unverified": 0} for f, m in modes.items()}
            if result is not None and result.status is SearchJobSourceStatus.SUCCEEDED:
                links = await self._filter(db, result.vacancy_ids, claim.params, modes, report)

            values: dict[str, Any] = {
                "status": status,
                "error_code": result.error_code if result else SourceErrorCode.DISABLED,
                "fetched_count": result.fetched if result else 0,
                "duration_ms": result.duration_ms if result else 0,
                "finished_at": now,
                "filter_report": report,
            }
            linked = 0
            if job.status is SearchJobStatus.RUNNING and links:
                linked = await repo.link_results(
                    job_id, links, cap=self._settings.search_max_results_per_job
                )
            values["stored_count"] = linked
            if not await repo.finish_source(
                job_id, source_id, attempt=claim.attempt, values=values
            ):
                await db.rollback()  # superseded claim or cancelled run: drop this outcome
                logger.info("source_outcome_discarded", job_id=str(job_id), source=claim.source_key)
                return
            await job_lifecycle.refresh_progress(repo, job)
            await db.commit()
        logger.info(
            "search_job_source_finished",
            job_id=str(job_id),
            source=claim.source_key,
            status=status,
            error_code=values["error_code"],
            attempt=claim.attempt,
            linked=linked,
        )

    async def _filter(
        self,
        db: AsyncSession,
        vacancy_ids: list[uuid.UUID],
        params: SearchParamsV1,
        modes: dict[SearchFilter, FilterMode],
        report: dict[SearchFilter, dict[str, Any]],
    ) -> list[tuple[uuid.UUID, list[str]]]:
        if not vacancy_ids:
            return []
        vacancies = {
            vacancy.id: vacancy
            for vacancy in await db.scalars(select(Vacancy).where(Vacancy.id.in_(vacancy_ids)))
        }
        now = self._now()
        links: list[tuple[uuid.UUID, list[str]]] = []
        for vacancy_id in dict.fromkeys(vacancy_ids):  # keep arrival order, drop repeats
            vacancy = vacancies.get(vacancy_id)
            if vacancy is None:
                continue
            decision = search_filters.evaluate(vacancy, params, modes, now)
            if not decision.include:
                if decision.excluded_by is not None:
                    report[decision.excluded_by]["excluded"] += 1
                continue
            for search_filter in decision.unverified:
                report[search_filter]["unverified"] += 1
            links.append((vacancy_id, [f.value for f in decision.unverified]))
        return links
