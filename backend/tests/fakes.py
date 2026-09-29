"""Test doubles for app dependencies that the lifespan normally creates (Redis, SMTP)."""

import time
from collections import defaultdict
from collections.abc import Callable

from fastapi import FastAPI

from app.core.rate_limit import RateLimitDecision, RateLimitRule
from app.integrations.email.base import OutgoingEmail


class AllowAllLimiter:
    """Default in API tests, which sign up many users from one client address."""

    async def check(self, rule: RateLimitRule, key: str) -> RateLimitDecision:
        return RateLimitDecision(allowed=True)

    async def hit(self, rule: RateLimitRule, key: str) -> RateLimitDecision:
        return RateLimitDecision(allowed=True)


class InMemoryRateLimiter:
    """Same sliding-window semantics as RedisRateLimiter, for tests of the real rules."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.hits: dict[tuple[str, str], list[float]] = defaultdict(list)

    async def check(self, rule: RateLimitRule, key: str) -> RateLimitDecision:
        return self._decide(rule, key, count=False)

    async def hit(self, rule: RateLimitRule, key: str) -> RateLimitDecision:
        return self._decide(rule, key, count=True)

    def _decide(self, rule: RateLimitRule, key: str, *, count: bool) -> RateLimitDecision:
        now = self.clock()
        window = rule.window.total_seconds()
        times = [t for t in self.hits[(rule.name, key)] if t > now - window]
        self.hits[(rule.name, key)] = times
        if len(times) < rule.limit:
            if count:
                times.append(now)
            return RateLimitDecision(allowed=True)
        return RateLimitDecision(
            allowed=False, retry_after_seconds=max(1, int(times[0] + window - now))
        )


class RecordingEmailSender:
    def __init__(self) -> None:
        self.sent: list[OutgoingEmail] = []

    async def send(self, email: OutgoingEmail) -> None:
        self.sent.append(email)


def install_test_doubles(app: FastAPI) -> None:
    app.state.auth_rate_limiter = AllowAllLimiter()
    app.state.email_sender = RecordingEmailSender()
