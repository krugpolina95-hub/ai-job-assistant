import time
import uuid

from app.core.ids import uuid7


def test_uuid7_sets_version_and_rfc_variant() -> None:
    value = uuid7()

    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_uuid7_embeds_current_unix_milliseconds() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000

    assert before <= value.int >> 80 <= after


def test_uuid7_sorts_by_creation_time_across_milliseconds() -> None:
    first = uuid7()
    time.sleep(0.002)
    second = uuid7()

    assert first < second


def test_uuid7_is_unique() -> None:
    assert len({uuid7() for _ in range(10_000)}) == 10_000
