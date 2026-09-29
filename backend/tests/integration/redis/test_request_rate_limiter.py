"""Request rate limits against a real Redis (TD-18, architecture §4.4): sliding window."""

import asyncio
from datetime import timedelta

import pytest
import structlog
from redis.asyncio import Redis

from app.core.rate_limit import RateLimitDecision, RateLimitRule, RedisRateLimiter

pytestmark = pytest.mark.asyncio(loop_scope="session")

THREE_PER_MINUTE = RateLimitRule("test_rule", 3, timedelta(minutes=1))
SECRET = "rate-limit-test-secret-0123456789abcdef"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def limiter(redis: Redis, prefix: str, clock: FakeClock) -> RedisRateLimiter:
    return RedisRateLimiter(redis, key_secret=SECRET, clock=clock, prefix=prefix)


async def test_counts_hits_up_to_the_limit(redis: Redis, key_prefix: str) -> None:
    clock = FakeClock()
    rate = limiter(redis, key_prefix, clock)

    for _ in range(3):
        assert (await rate.hit(THREE_PER_MINUTE, "k")).allowed
        clock.now += 1

    refused = await rate.hit(THREE_PER_MINUTE, "k")
    assert refused == RateLimitDecision(allowed=False, retry_after_seconds=57)


async def test_check_does_not_count(redis: Redis, key_prefix: str) -> None:
    rate = limiter(redis, key_prefix, FakeClock())

    for _ in range(10):
        assert (await rate.check(THREE_PER_MINUTE, "k")).allowed
    for _ in range(3):
        await rate.hit(THREE_PER_MINUTE, "k")

    assert not (await rate.check(THREE_PER_MINUTE, "k")).allowed


async def test_window_slides(redis: Redis, key_prefix: str) -> None:
    clock = FakeClock()
    start = clock.now
    rate = limiter(redis, key_prefix, clock)
    for offset in (0, 20, 40):
        clock.now = start + offset
        await rate.hit(THREE_PER_MINUTE, "k")

    clock.now = start + 59
    assert not (await rate.check(THREE_PER_MINUTE, "k")).allowed
    clock.now = start + 61  # the first hit has left the window
    assert (await rate.hit(THREE_PER_MINUTE, "k")).allowed
    assert not (await rate.hit(THREE_PER_MINUTE, "k")).allowed


async def test_refused_attempts_are_not_counted(redis: Redis, key_prefix: str) -> None:
    clock = FakeClock()
    rate = limiter(redis, key_prefix, clock)
    for _ in range(3):
        await rate.hit(THREE_PER_MINUTE, "k")
    for _ in range(20):
        await rate.hit(THREE_PER_MINUTE, "k")

    clock.now += 61
    for _ in range(3):
        assert (await rate.hit(THREE_PER_MINUTE, "k")).allowed


async def test_keys_and_rules_are_independent(redis: Redis, key_prefix: str) -> None:
    rate = limiter(redis, key_prefix, FakeClock())
    other_rule = RateLimitRule("other_rule", 3, timedelta(minutes=1))
    for _ in range(3):
        await rate.hit(THREE_PER_MINUTE, "a")

    assert (await rate.hit(THREE_PER_MINUTE, "b")).allowed
    assert (await rate.hit(other_rule, "a")).allowed


async def test_concurrent_hits_never_exceed_the_limit(redis: Redis, key_prefix: str) -> None:
    rate = limiter(redis, key_prefix, FakeClock())

    decisions = await asyncio.gather(*(rate.hit(THREE_PER_MINUTE, "k") for _ in range(30)))

    assert sum(decision.allowed for decision in decisions) == 3


async def test_redis_never_sees_the_raw_key(redis: Redis, key_prefix: str) -> None:
    rate = limiter(redis, key_prefix, FakeClock())

    await rate.hit(THREE_PER_MINUTE, "203.0.113.9|ada@example.com")

    keys = [key.decode() async for key in redis.scan_iter(match=f"{key_prefix}:*")]
    assert len(keys) == 1
    assert "203.0.113" not in keys[0]
    assert "ada" not in keys[0]
    assert await redis.pttl(keys[0]) > 0


async def test_unreachable_redis_lets_requests_through() -> None:
    broken = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    rate = RedisRateLimiter(broken, key_secret=SECRET)

    with structlog.testing.capture_logs() as logs:
        decision = await rate.hit(THREE_PER_MINUTE, "k")
    await broken.aclose()

    assert decision.allowed
    assert [entry["event"] for entry in logs] == ["rate_limiter_unavailable"]
