"""Internal API contract between n8n and the backend (GAP-09, architecture §8.3).

n8n gets only what it needs to orchestrate: ids, statuses, source keys and counts. Search
parameters and the profile snapshot stay in the backend: n8n doesn't run adapters, so it needs
no user data (data minimization).
"""

import uuid
from datetime import datetime
from typing import Literal

from app.core.enums import SearchJobSourceStatus, SearchJobStatus
from app.schemas.base import InputSchema, OutputSchema


class JobContextOut(OutputSchema):
    job_id: uuid.UUID
    status: SearchJobStatus
    deadline_at: datetime
    sources: list[str]  # source keys to run, one WF-002 call each


class JobStatusIn(InputSchema):
    status: Literal["running"]


class JobStatusOut(OutputSchema):
    job_id: uuid.UUID
    status: SearchJobStatus


class SourceRunOut(OutputSchema):
    job_id: uuid.UUID
    source: str
    status: SearchJobSourceStatus
    attempts: int
    fetched_count: int
    stored_count: int
    error_code: str | None


class FinalizeOut(OutputSchema):
    job_id: uuid.UUID
    status: SearchJobStatus
    result_count: int
    error_code: str | None


class SweepOut(OutputSchema):
    finalized: int
