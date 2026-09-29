"""How a created search run gets executed (TD-09).

`InlineDispatcher` runs it in this process (M5, dev, tests, emergency fallback). An
`N8nDispatcher` arrives in M6 and will drive the same `SearchJobRunner` steps through
internal endpoints. The search-run logic never depends on which one is used.
"""

import asyncio
import uuid
from typing import Protocol

import structlog

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
