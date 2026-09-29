"""How a created search run gets executed (TD-09).

`InlineDispatcher` runs it in this process (dev, tests, emergency fallback). `N8nDispatcher`
triggers n8n, which drives the same `SearchJobRunner` steps through /internal/v1
(JOB_DISPATCHER=n8n). The search-run logic never depends on which one is used.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.integrations.n8n.webhook import N8nTriggerError, N8nWebhookClient
from app.services import job_lifecycle
from app.services.job_runner import SearchJobRunner

logger = structlog.stdlib.get_logger(__name__)


class JobDispatcher(Protocol):
    async def dispatch(self, job_id: uuid.UUID) -> None:
        """Hand a committed, pending run over for execution. Must return quickly."""
        ...


class InlineDispatcher:
    def __init__(self, runner: SearchJobRunner) -> None:
        self._runner = runner
        self._tasks: set[asyncio.Task[None]] = set()

    async def dispatch(self, job_id: uuid.UUID) -> None:
        task = asyncio.create_task(self._run(job_id), name=f"search-job-{job_id}")
        self._tasks.add(task)  # keep a reference until done (asyncio holds only weak ones)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Waits for running jobs (shutdown, tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _run(self, job_id: uuid.UUID) -> None:
        try:
            await self._runner.run(job_id)
        except Exception:
            # The job stays active; its deadline finalizes it on the next read (NFR-07).
            logger.exception("search_job_crashed", job_id=str(job_id))


DISPATCH_ERROR = "DISPATCH_ERROR"


class N8nDispatcher:
    """Triggers WF-001, which drives the run through /internal/v1. One retry covers a brief
    n8n hiccup; if the trigger still fails the run is ended at once as `failed` with
    `DISPATCH_ERROR` (TD-09) instead of waiting for its deadline. A trigger that reached n8n
    twice is harmless: every internal step is idempotent."""

    def __init__(
        self,
        webhook: N8nWebhookClient,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        retry_delay_seconds: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._webhook = webhook
        self._sessions = session_factory
        self._retry_delay = retry_delay_seconds
        self._sleep = sleep

    async def dispatch(self, job_id: uuid.UUID) -> None:
        request_id = structlog.contextvars.get_contextvars().get("request_id")
        for attempt in (1, 2):
            try:
                await self._webhook.trigger(job_id, request_id)
            except N8nTriggerError as exc:
                logger.warning(
                    "search_job_dispatch_failed",
                    job_id=str(job_id),
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt == 1:
                    await self._sleep(self._retry_delay)
                continue
            logger.info("search_job_dispatched", job_id=str(job_id), dispatcher="n8n")
            return
        async with self._sessions() as db:
            await job_lifecycle.fail(db, job_id, DISPATCH_ERROR, datetime.now(UTC))
