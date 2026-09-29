"""Per-source API rate limiting (TD-08, TD-18), shared by all backend processes via Redis.

Each source has a token bucket (e.g. Adzuna: 25 requests per 60 s) and optionally a daily
quota (Adzuna: 250/day, resetting at 00:00 UTC). One atomic Lua script decides per request.
This is separate from user-level search-run limits, which live in the job service.
"""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.integrations.sources.base import (
    RateLimit,
    SourceQuotaExhaustedError,
    SourceRateLimitedError,
    SourceRateLimiterUnavailableError,
)

# KEYS: bucket, daily counter. ARGV: capacity, refill per ms, now ms, daily limit (0 = none),
# daily key TTL s, bucket TTL ms. Returns {1, 0} allowed | {0, wait_ms} | {0, -1} quota spent.
_BUCKET_SCRIPT = """
local capacity = tonumber(ARGV[1])
local refill_per_ms = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local daily_limit = tonumber(ARGV[4])
if daily_limit > 0 and tonumber(redis.call('GET', KEYS[2]) or '0') >= daily_limit then
  return {0, -1}
end
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or capacity
local last = tonumber(state[2]) or now_ms
tokens = math.min(capacity, tokens + math.max(0, now_ms - last) * refill_per_ms)
if tokens >= 1 then
  redis.call('HSET', KEYS[1], 'tokens', tostring(tokens - 1), 'ts', tostring(now_ms))
  redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[6]))
  if daily_limit > 0 then
    redis.call('INCR', KEYS[2])
    redis.call('EXPIRE', KEYS[2], tonumber(ARGV[5]))
  end
  return {1, 0}
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now_ms))
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[6]))
return {0, math.ceil((1 - tokens) / refill_per_ms)}
"""


class SourceRateLimiter(Protocol):
    async def acquire(self, source_key: str, limit: RateLimit, *, max_wait: float) -> None:
        """Returns when one request may be sent. Raises SourceRateLimitedError when the wait
        would exceed `max_wait`, SourceQuotaExhaustedError when today's quota is spent, and
        SourceRateLimiterUnavailableError when the limiter itself can't be reached."""
        ...


class RedisSourceRateLimiter:
    def __init__(
        self,
        redis: Redis,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        prefix: str = "ratelimit:source",
    ) -> None:
        self._script = redis.register_script(_BUCKET_SCRIPT)
        self._clock = clock
        self._sleep = sleep
        self._prefix = prefix

    async def acquire(self, source_key: str, limit: RateLimit, *, max_wait: float) -> None:
        waited = 0.0
        while True:
            now = self._clock()
            day = datetime.fromtimestamp(now, UTC)
            end_of_day = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
            refill_per_ms = limit.requests / (limit.per_seconds * 1000)
            try:
                allowed, wait_ms = await self._script(
                    keys=[
                        f"{self._prefix}:{source_key}:bucket",
                        f"{self._prefix}:{source_key}:day:{day:%Y%m%d}",
                    ],
                    args=[
                        limit.requests,
                        repr(refill_per_ms),
                        int(now * 1000),
                        limit.requests_per_day or 0,
                        int((end_of_day - day).total_seconds()) + 3600,
                        math.ceil(limit.per_seconds * 1000 * 2),
                    ],
                )
            except RedisError:
                raise SourceRateLimiterUnavailableError(
                    f"Rate limiter for {source_key} is unavailable"
                ) from None
            if allowed:
                return
            if wait_ms < 0:
                raise SourceQuotaExhaustedError(f"Daily request quota for {source_key} is spent")
            wait = wait_ms / 1000
            if waited + wait > max_wait:
                raise SourceRateLimitedError(f"Request budget for {source_key} is spent for now")
            await self._sleep(wait)
            waited += wait
