import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import joinedload

from app.core.enums import ACTIVE_SEARCH_JOB_STATUSES, SearchJobSourceStatus, SearchJobStatus
from app.models import Search, SearchJob, SearchJobResult, SearchJobSource, User
from app.repositories.base import UserOwnedRepository


class SearchRepository(UserOwnedRepository[Search]):
    model = Search

    async def exists_for_user(self, user_id: uuid.UUID) -> bool:
        found = await self.session.scalar(
            select(select(Search.id).where(Search.user_id == user_id).exists())
        )
        return bool(found)


class SearchJobRepository(UserOwnedRepository[SearchJob]):
    """Search runs. User-facing reads take `user_id`; the `*_for_runner` methods are used by
    the dispatcher, which acts for the system on a job id it created itself."""

    model = SearchJob

    # --- creation guards (TD-12) ---------------------------------------------------------

    async def lock_user(self, user_id: uuid.UUID) -> None:
        """Serializes run creation per user, so the active-run limit can't be raced."""
        await self.session.execute(select(User.id).where(User.id == user_id).with_for_update())

    async def get_by_idempotency_key(self, user_id: uuid.UUID, key: str) -> SearchJob | None:
        job: SearchJob | None = await self.session.scalar(
            select(SearchJob).where(SearchJob.user_id == user_id, SearchJob.idempotency_key == key)
        )
        return job

    async def get_active_for_search(
        self, user_id: uuid.UUID, search_id: uuid.UUID
    ) -> SearchJob | None:
        job: SearchJob | None = await self.session.scalar(
            select(SearchJob).where(
                SearchJob.user_id == user_id,
                SearchJob.search_id == search_id,
                SearchJob.status.in_(ACTIVE_SEARCH_JOB_STATUSES),
            )
        )
        return job

    async def count_active(self, user_id: uuid.UUID) -> int:
        count = await self.session.scalar(
            select(func.count())
            .select_from(SearchJob)
            .where(SearchJob.user_id == user_id, SearchJob.status.in_(ACTIVE_SEARCH_JOB_STATUSES))
        )
        return count or 0

    async def count_created_since(self, user_id: uuid.UUID, since: datetime) -> int:
        count = await self.session.scalar(
            select(func.count())
            .select_from(SearchJob)
            .where(SearchJob.user_id == user_id, SearchJob.created_at >= since)
        )
        return count or 0

    # --- reads ---------------------------------------------------------------------------

    async def list_for_search(
        self,
        user_id: uuid.UUID,
        search_id: uuid.UUID,
        *,
        before: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[SearchJob]:
        statement = (
            select(SearchJob)
            .where(SearchJob.user_id == user_id, SearchJob.search_id == search_id)
            .order_by(SearchJob.created_at.desc(), SearchJob.id.desc())
            .limit(limit + 1)
        )
        if before is not None:
            created_at, job_id = before
            statement = statement.where(
                or_(
                    SearchJob.created_at < created_at,
                    and_(SearchJob.created_at == created_at, SearchJob.id < job_id),
                )
            )
        return list(await self.session.scalars(statement))

    async def list_sources(self, job_id: uuid.UUID) -> list[SearchJobSource]:
        result = await self.session.scalars(
            select(SearchJobSource)
            .options(joinedload(SearchJobSource.source))
            .where(SearchJobSource.search_job_id == job_id)
            .order_by(SearchJobSource.created_at, SearchJobSource.source_id)
        )
        return list(result)

    async def list_overdue_ids(self, now: datetime, limit: int) -> list[uuid.UUID]:
        """Active runs past their deadline, oldest first (served by the partial deadline index)."""
        result = await self.session.scalars(
            select(SearchJob.id)
            .where(SearchJob.status.in_(ACTIVE_SEARCH_JOB_STATUSES), SearchJob.deadline_at <= now)
            .order_by(SearchJob.deadline_at)
            .limit(limit)
        )
        return list(result)

    async def get_by_id_for_runner(self, job_id: uuid.UUID) -> SearchJob | None:
        return await self.session.get(SearchJob, job_id)

    async def lock_for_runner(self, job_id: uuid.UUID) -> SearchJob | None:
        """The job row, locked until commit: status changes, linking and progress for one job
        are serialized, whatever process or worker performs them."""
        job: SearchJob | None = await self.session.scalar(
            select(SearchJob).where(SearchJob.id == job_id).with_for_update()
        )
        return job

    # --- source runs ---------------------------------------------------------------------

    async def claim_source(
        self,
        job_id: uuid.UUID,
        source_id: uuid.UUID,
        *,
        now: datetime,
        max_attempts: int,
        retryable_codes: list[str],
        stale_before: datetime,
    ) -> int | None:
        """Atomically moves one source run to `running` and returns its attempt number (the
        fencing token), or None if it is not claimable: already done, a final failure, out of
        attempts, claimed by a live worker, or its job isn't running before the deadline."""
        claimable = or_(
            SearchJobSource.status == SearchJobSourceStatus.PENDING,
            and_(
                SearchJobSource.status.in_(
                    [SearchJobSourceStatus.FAILED, SearchJobSourceStatus.TIMED_OUT]
                ),
                SearchJobSource.error_code.in_(retryable_codes),
            ),
            and_(
                SearchJobSource.status == SearchJobSourceStatus.RUNNING,
                SearchJobSource.started_at < stale_before,  # the previous worker vanished
            ),
        )
        job_is_running = (
            select(SearchJob.id)
            .where(
                SearchJob.id == job_id,
                SearchJob.status == SearchJobStatus.RUNNING,
                SearchJob.deadline_at > now,
            )
            .exists()
        )
        attempt: int | None = await self.session.scalar(
            update(SearchJobSource)
            .where(
                SearchJobSource.search_job_id == job_id,
                SearchJobSource.source_id == source_id,
                SearchJobSource.attempts < max_attempts,
                claimable,
                job_is_running,
            )
            .values(
                status=SearchJobSourceStatus.RUNNING,
                attempts=SearchJobSource.attempts + 1,
                started_at=now,
                finished_at=None,
                error_code=None,
            )
            .returning(SearchJobSource.attempts)
        )
        return attempt

    async def get_source_run(
        self, job_id: uuid.UUID, source_id: uuid.UUID
    ) -> SearchJobSource | None:
        row: SearchJobSource | None = await self.session.scalar(
            select(SearchJobSource)
            .options(joinedload(SearchJobSource.source))
            .where(SearchJobSource.search_job_id == job_id, SearchJobSource.source_id == source_id)
        )
        return row

    async def finish_source(
        self, job_id: uuid.UUID, source_id: uuid.UUID, *, attempt: int, values: dict[str, Any]
    ) -> bool:
        """Records an outcome only for the claim that produced it (fencing): a worker whose
        claim was taken over can't overwrite the newer run."""
        updated = await self.session.scalar(
            update(SearchJobSource)
            .where(
                SearchJobSource.search_job_id == job_id,
                SearchJobSource.source_id == source_id,
                SearchJobSource.status == SearchJobSourceStatus.RUNNING,
                SearchJobSource.attempts == attempt,
            )
            .values(**values)
            .returning(SearchJobSource.source_id)
        )
        return updated is not None

    # --- results -------------------------------------------------------------------------

    async def count_results(self, job_id: uuid.UUID) -> int:
        count = await self.session.scalar(
            select(func.count())
            .select_from(SearchJobResult)
            .where(SearchJobResult.search_job_id == job_id)
        )
        return count or 0

    async def link_results(
        self, job_id: uuid.UUID, links: list[tuple[uuid.UUID, list[str]]], *, cap: int
    ) -> int:
        """Links vacancies to the run in arrival order, idempotently: a vacancy already linked
        (retry, re-sighting) keeps its first link. Never exceeds `cap` results per run.
        Call with the job row locked, so ranks don't collide."""
        existing = set(
            await self.session.scalars(
                select(SearchJobResult.vacancy_id).where(SearchJobResult.search_job_id == job_id)
            )
        )
        next_rank = len(existing) + 1
        rows = []
        for vacancy_id, unverified in links:
            if vacancy_id in existing or next_rank > cap:
                continue
            existing.add(vacancy_id)
            rows.append(
                {
                    "search_job_id": job_id,
                    "vacancy_id": vacancy_id,
                    "rank": next_rank,
                    "unverified_filters": unverified,
                }
            )
            next_rank += 1
        if rows:
            await self.session.execute(
                insert(SearchJobResult).values(rows).on_conflict_do_nothing()
            )
        return len(rows)

    async def unverified_filters_for(
        self, job_id: uuid.UUID, vacancy_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[str]]:
        if not vacancy_ids:
            return {}
        rows = await self.session.execute(
            select(SearchJobResult.vacancy_id, SearchJobResult.unverified_filters).where(
                SearchJobResult.search_job_id == job_id,
                SearchJobResult.vacancy_id.in_(vacancy_ids),
            )
        )
        return {vacancy_id: list(filters) for vacancy_id, filters in rows.all()}
