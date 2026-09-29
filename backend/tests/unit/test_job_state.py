import pytest

from app.core.enums import SearchJobSourceStatus as S
from app.core.enums import SearchJobStatus as J
from app.services.job_state import JobErrorCode, can_transition, final_status, is_terminal


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (J.PENDING, J.RUNNING, True),
        (J.PENDING, J.CANCELLED, True),
        (J.PENDING, J.FAILED, True),
        (J.RUNNING, J.COMPLETED, True),
        (J.RUNNING, J.PARTIALLY_COMPLETED, True),
        (J.RUNNING, J.CANCELLED, True),
        (J.RUNNING, J.RUNNING, True),  # repeating is harmless
        (J.RUNNING, J.PENDING, False),
        (J.COMPLETED, J.RUNNING, False),
        (J.COMPLETED, J.CANCELLED, False),
        (J.CANCELLED, J.RUNNING, False),
        (J.FAILED, J.COMPLETED, False),
        (J.COMPLETED, J.COMPLETED, True),
    ],
)
def test_transitions(current: J, target: J, allowed: bool) -> None:
    assert can_transition(current, target) is allowed


def test_terminal_statuses() -> None:
    assert {s for s in J if is_terminal(s)} == {
        J.COMPLETED,
        J.PARTIALLY_COMPLETED,
        J.FAILED,
        J.CANCELLED,
    }


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        ([S.SUCCEEDED, S.SUCCEEDED], (J.COMPLETED, None)),
        ([S.SUCCEEDED], (J.COMPLETED, None)),  # 0 results still counts as success
        ([S.SUCCEEDED, S.FAILED], (J.PARTIALLY_COMPLETED, None)),
        ([S.SUCCEEDED, S.TIMED_OUT], (J.PARTIALLY_COMPLETED, None)),
        ([S.FAILED, S.TIMED_OUT], (J.FAILED, JobErrorCode.ALL_SOURCES_FAILED)),
        ([S.SUCCEEDED, S.SKIPPED], (J.COMPLETED, None)),  # skipped doesn't count
        ([S.FAILED, S.SKIPPED], (J.FAILED, JobErrorCode.ALL_SOURCES_FAILED)),
        ([S.SKIPPED, S.SKIPPED], (J.FAILED, JobErrorCode.NO_SOURCES_RAN)),
        ([], (J.FAILED, JobErrorCode.NO_SOURCES_RAN)),
    ],
)
def test_final_status_follows_amb_23(sources: list[S], expected: tuple[J, object]) -> None:
    assert final_status(sources) == expected
