"""Per-source rate limits against a real Redis (TD-18): token bucket + daily quota."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis

from app.integrations.sources.base import (
    RateLimit,
    SourceQuotaExhaustedError,
    SourceRateLimitedError,
    SourceRateLimiterUnavailableError,
)
from app.integrations.sources.rate_limit import RedisSourceRateLimiter

pytestmark = pytest.mark.asyncio(loop_scope="session")

FIVE_PER_SECOND = RateLimit(requests=5, per_seconds=1)


class FakeClock:
    """Controls time; `sleep` advances it instead of waiting."""

    def __init__(self, start: datetime = datetime(2026, 9, 28, 12, tzinfo=UTC)) -> None:
        self.now = start.timestamp()
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def limiter(redis: Redis, prefix: str, clock: FakeClock) -> RedisSourceRateLimiter:
    return RedisSourceRateLimiter(redis, clock=clock, sleep=clock.sleep, prefix=prefix)


async def test_burst_up_to_capacity_then_limited(redis: Redis, key_prefix: str) -> None:
    clock = FakeClock()
    rate = limiter(redis, key_prefix, clock)

    for _ in range(5):
        await rate.acquire("adzuna", FIVE_PER_SECOND, max_wait=0)
    with pytest.raises(SourceRateLimitedError):
        await rate.acquire("adzuna", FIVE_PER_SECOND, max_wait=0)


async def test_tokens_refill_over_time(redis: Redis, key_prefix: str) -> None:
    clock = FakeClock()
    rate = limiter(redis, key_prefix, clock)
    for _ in range(5):
        await rate.acquire("adzuna", FIVE_PER_SECOND, max_wait=0)

    clock.now += 0.2  # one token at 5/s
    await rate.acquire("adzuna", FIVE_PER_SECOND, max_wait=0)
    with pytest.raises(SourceRateLimitedError):
        await rate.acquire("adzuna", FIVE_PER_SECOND, max_wait=0)


async def test_short_waits_are_taken_within_max_wait(redis: Redis, key_prefix: str) -> None:
    clock = FakeClock()
    rate = limiter(redis, key_prefix, clock)
    for _ in range(5):
        await rate.acquire("adzuna", FIVE_PER_SECOND, max_wait=0)

    await rate.acquire("adzuna", FIVE_PER_SECOND, max_wait=1)

    assert clock.slept and 0 < sum(clock.slept) <= 0.21


async def test_daily_quota_is_final_until_the_next_utc_day(redis: Redis, key_prefix: str) -> None:
    clock = FakeClock()
    rate = limiter(redis, key_prefix, clock)
    quota = RateLimit(requests=100, per_seconds=60, requests_per_day=3)
    for _ in range(3):
        await rate.acquire("adzuna", quota, max_wait=0)

    with pytest.raises(SourceQuotaExhaustedError):
        await rate.acquire("adzuna", quota, max_wait=60)  # waiting wouldn't help
    assert clock.slept == []

    clock.now = (datetime(2026, 9, 29, 0, 0, 1, tzinfo=UTC)).timestamp()
    await rate.acquire("adzuna", quota, max_wait=0)


async def test_daily_counter_expires(redis: Redis, key_prefix: str) -> None:
    clock = FakeClock()
    await limiter(redis, key_prefix, clock).acquire(
        "adzuna", RateLimit(requests=10, per_seconds=60, requests_per_day=10), max_wait=0
    )

    ttl = await redis.ttl(f"{key_prefix}:adzuna:day:20260928")
    assert 0 < ttl <= int(timedelta(hours=13).total_seconds())


async def test_sources_have_separate_budgets(redis: Redis, key_prefix: str) -> None:
    clock = FakeClock()
    rate = limiter(redis, key_prefix, clock)
    for _ in range(5):
        await rate.acquire("adzuna", FIVE_PER_SECOND, max_wait=0)

    await rate.acquire("other-source", FIVE_PER_SECOND, max_wait=0)


async def test_concurrent_acquires_never_exceed_capacity(redis: Redis, key_prefix: str) -> None:
    clock = FakeClock()
    rate = limiter(redis, key_prefix, clock)
    ten_per_minute = RateLimit(requests=10, per_seconds=60)

    async def attempt() -> bool:
        try:
            await rate.acquire("adzuna", ten_per_minute, max_wait=0)
        except SourceRateLimitedError:
            return False
        return True

    granted = await asyncio.gather(*(attempt() for _ in range(50)))

    assert sum(granted) == 10


async def test_unreachable_redis_fails_closed() -> None:
    unreachable = Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    rate = RedisSourceRateLimiter(unreachable)

    with pytest.raises(SourceRateLimiterUnavailableError) as error:
        await rate.acquire("adzuna", FIVE_PER_SECOND, max_wait=0)

    assert error.value.__cause__ is None
    await unreachable.aclose()
