"""Request rate limits shared by all backend processes (TD-18, architecture §4.4).

A sliding-window log in Redis: one sorted set per rule and key, holding the timestamps of the
counted attempts inside the window. `check` only looks; `hit` counts one attempt if the limit
allows it. Keys are HMAC-hashed, so Redis never holds a raw email or IP address.

If Redis is unreachable the limiter lets requests through and logs it: Redis is a cache here
(TD-18), and the account lockout in the database keeps protecting logins.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.ids import uuid7
from app.core.security import hash_identifier

logger = structlog.stdlib.get_logger(__name__)


@dataclass(frozen=True)
class RateLimitRule:
    name: str
    limit: int
    window: timedelta


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


class RateLimiter(Protocol):
    async def check(self, rule: RateLimitRule, key: str) -> RateLimitDecision:
        """Whether one more attempt would be allowed now, without counting it."""
        ...

    async def hit(self, rule: RateLimitRule, key: str) -> RateLimitDecision:
        """Counts one attempt if allowed; a refused attempt is not counted."""
        ...


# KEYS: the rule's sorted set. ARGV: now ms, window ms, limit, member ("" = only check).
# Returns {1, 0} allowed | {0, retry_after_ms}.
_SLIDING_WINDOW_SCRIPT = """
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms - window_ms)
if redis.call('ZCARD', KEYS[1]) < limit then
  if ARGV[4] ~= '' then
    redis.call('ZADD', KEYS[1], now_ms, ARGV[4])
    redis.call('PEXPIRE', KEYS[1], window_ms)
  end
  return {1, 0}
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return {0, tonumber(oldest[2]) + window_ms - now_ms}
"""


class RedisRateLimiter:
    def __init__(
        self,
        redis: Redis,
        *,
        key_secret: str,
        clock: Callable[[], float] = time.time,
        prefix: str = "ratelimit",
    ) -> None:
        self._script = redis.register_script(_SLIDING_WINDOW_SCRIPT)
        self._key_secret = key_secret
        self._clock = clock
        self._prefix = prefix

    async def check(self, rule: RateLimitRule, key: str) -> RateLimitDecision:
        return await self._run(rule, key, count=False)

    async def hit(self, rule: RateLimitRule, key: str) -> RateLimitDecision:
        return await self._run(rule, key, count=True)

    async def _run(self, rule: RateLimitRule, key: str, *, count: bool) -> RateLimitDecision:
        now_ms = int(self._clock() * 1000)
        window_ms = int(rule.window.total_seconds() * 1000)
        redis_key = f"{self._prefix}:{rule.name}:{hash_identifier(key, self._key_secret)}"
        try:
            allowed, retry_after_ms = await self._script(
                keys=[redis_key],
                args=[now_ms, window_ms, rule.limit, uuid7().hex if count else ""],
            )
        except RedisError:
            logger.warning("rate_limiter_unavailable", rule=rule.name)
            return RateLimitDecision(allowed=True)
        if allowed:
            return RateLimitDecision(allowed=True)
        return RateLimitDecision(
            allowed=False, retry_after_seconds=max(1, math.ceil(retry_after_ms / 1000))
        )
