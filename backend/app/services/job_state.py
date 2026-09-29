"""Search-run state rules (architecture §6.2, AMB-23). Pure: no I/O."""

from collections.abc import Iterable
from enum import StrEnum

from app.core.enums import TERMINAL_SEARCH_JOB_STATUSES, SearchJobSourceStatus, SearchJobStatus

_ALLOWED: dict[SearchJobStatus, frozenset[SearchJobStatus]] = {
    SearchJobStatus.PENDING: frozenset(
        {SearchJobStatus.RUNNING, SearchJobStatus.CANCELLED, *TERMINAL_SEARCH_JOB_STATUSES}
    ),
    SearchJobStatus.RUNNING: frozenset(TERMINAL_SEARCH_JOB_STATUSES),
}

FINISHED_SOURCE_STATUSES = frozenset(
    {
        SearchJobSourceStatus.SUCCEEDED,
        SearchJobSourceStatus.FAILED,
        SearchJobSourceStatus.TIMED_OUT,
        SearchJobSourceStatus.SKIPPED,
    }
)


class JobErrorCode(StrEnum):
    ALL_SOURCES_FAILED = "ALL_SOURCES_FAILED"
    NO_SOURCES_RAN = "NO_SOURCES_RAN"


def can_transition(current: SearchJobStatus, target: SearchJobStatus) -> bool:
    """Repeating the current status is allowed (idempotent); terminal states never change."""
    return current == target or target in _ALLOWED.get(current, frozenset())


def is_terminal(status: SearchJobStatus) -> bool:
    return status in TERMINAL_SEARCH_JOB_STATUSES


def final_status(
    source_statuses: Iterable[SearchJobSourceStatus],
) -> tuple[SearchJobStatus, JobErrorCode | None]:
    """AMB-23: every source succeeded (even with 0 results) → completed; at least one succeeded
    and at least one failed or timed out → partially_completed; none succeeded → failed.
    Skipped sources (disabled after the run was created) don't count."""
    ran = [status for status in source_statuses if status is not SearchJobSourceStatus.SKIPPED]
    if not ran:
        return SearchJobStatus.FAILED, JobErrorCode.NO_SOURCES_RAN
    succeeded = sum(status is SearchJobSourceStatus.SUCCEEDED for status in ran)
    if succeeded == len(ran):
        return SearchJobStatus.COMPLETED, None
    if succeeded:
        return SearchJobStatus.PARTIALLY_COMPLETED, None
    return SearchJobStatus.FAILED, JobErrorCode.ALL_SOURCES_FAILED
