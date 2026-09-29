"""Test doubles for app dependencies that the lifespan normally creates (Redis, SMTP) and
for the AI provider (tests never call a real one, CLAUDE.md §8)."""

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from app.core.rate_limit import RateLimitDecision, RateLimitRule
from app.integrations.ai.base import ChatMessage, ModelTier, ProviderReply, TokenUsage
from app.integrations.email.base import OutgoingEmail


@dataclass(frozen=True)
class Hang:
    """A scripted provider call that doesn't answer for `seconds`."""

    seconds: float


@dataclass(frozen=True)
class ProviderCall:
    messages: list[ChatMessage]
    schema: Mapping[str, Any]
    tier: ModelTier


class FakeLLMProvider:
    """Recorded AI replies: each call takes the next scripted outcome, a reply (dict or JSON
    text), an exception to raise, or a `Hang`."""

    name = "fake"

    def __init__(
        self, *script: object, model: str = "fake-model-1", usage: TokenUsage | None = None
    ) -> None:
        self.script = list(script)
        self.model = model
        self.usage = usage or TokenUsage(input_tokens=100, output_tokens=20)
        self.calls: list[ProviderCall] = []

    async def generate_structured(
        self, *, messages: Sequence[ChatMessage], schema: Mapping[str, Any], tier: ModelTier
    ) -> ProviderReply:
        self.calls.append(ProviderCall(list(messages), schema, tier))
        if not self.script:
            raise AssertionError("FakeLLMProvider called more often than scripted")
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, Hang):
            await asyncio.sleep(outcome.seconds)
            raise AssertionError("a hanging call must be cut off by the timeout")
        if not isinstance(outcome, (str, Mapping)):
            raise TypeError(f"unsupported scripted outcome {outcome!r}")
        return ProviderReply(data=outcome, usage=self.usage, model=self.model)


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
