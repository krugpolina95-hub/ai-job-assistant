"""Guardrails around any `LLMProvider` (TD-07, AI-007): timeout, one retry on 429/5xx with
jitter, a circuit breaker, and Pydantic validation with one repair attempt.

Only validated models come out. The provider's raw reply is never returned, logged or put into
an exception, because it may repeat untrusted text (CLAUDE.md §7).
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from app.integrations.ai.base import (
    AIErrorReason,
    AIProviderError,
    AIProviderTimeoutError,
    ChatMessage,
    LLMProvider,
    ModelTier,
    ProviderReply,
    TokenUsage,
)

logger = structlog.stdlib.get_logger(__name__)


@dataclass(frozen=True)
class AIClientConfig:
    timeout_seconds: float = 20.0
    max_retries: int = 1
    retry_delay_seconds: float = 1.0
    # A provider's Retry-After above this is not waited for: the user is waiting.
    max_retry_after_seconds: float = 5.0
    circuit_failure_threshold: int = 5
    circuit_open_seconds: float = 60.0


@dataclass(frozen=True)
class AIResult[T: BaseModel]:
    value: T
    usage: TokenUsage
    model: str
    provider_calls: int
    latency_ms: int


class AICallError(Exception):
    """An AI call that produced no valid result. Safe to log: no prompt or reply text."""

    def __init__(
        self,
        reason: AIErrorReason,
        *,
        usage: TokenUsage = TokenUsage(),  # noqa: B008  (immutable)
        model: str | None = None,
        provider_calls: int = 0,
        latency_ms: int = 0,
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.usage = usage
        self.model = model
        self.provider_calls = provider_calls
        self.latency_ms = latency_ms


class CircuitBreaker:
    """Stops calling a failing provider for a while (TD-07: 5 failures in a row → 60 s).
    State is per process, which is enough to shed load from one worker."""

    def __init__(
        self, *, threshold: int, open_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._threshold = threshold
        self._open_seconds = open_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    def allows_call(self) -> bool:
        """Closed, or open long enough that one trial call may go through (half-open)."""
        return self._opened_at is None or self._clock() - self._opened_at >= self._open_seconds

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._clock()


class AIClient:
    def __init__(
        self,
        provider: LLMProvider,
        config: AIClientConfig,
        *,
        breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.provider = provider
        self._config = config
        self._breaker = breaker or CircuitBreaker(
            threshold=config.circuit_failure_threshold, open_seconds=config.circuit_open_seconds
        )
        self._sleep = sleep
        self._jitter = jitter

    async def generate[T: BaseModel](
        self, messages: Sequence[ChatMessage], output: type[T], tier: ModelTier
    ) -> AIResult[T]:
        if not self._breaker.allows_call():
            raise AICallError(AIErrorReason.CIRCUIT_OPEN)
        started = time.perf_counter()
        schema = output.model_json_schema()
        usage = TokenUsage()
        model: str | None = None
        calls = 0
        conversation = list(messages)
        # One normal attempt, then at most one repair attempt with the validation errors.
        for repair in (False, True):
            try:
                reply, attempts = await self._call_with_retry(conversation, schema, tier)
            except _CallFailed as failed:
                self._breaker.record_failure()
                raise AICallError(
                    failed.reason,
                    usage=usage,
                    model=model,
                    provider_calls=calls + failed.attempts,
                    latency_ms=_elapsed_ms(started),
                ) from None
            self._breaker.record_success()
            calls += attempts
            usage += reply.usage
            model = reply.model
            outcome = _validate(reply.data, output)
            if isinstance(outcome, BaseModel):
                return AIResult(outcome, usage, reply.model, calls, _elapsed_ms(started))
            logger.warning("ai_reply_invalid", repair_attempt=repair, problems=len(outcome))
            conversation = [*messages, _repair_request(outcome)]
        raise AICallError(
            AIErrorReason.INVALID_RESPONSE,
            usage=usage,
            model=model,
            provider_calls=calls,
            latency_ms=_elapsed_ms(started),
        )

    async def _call_with_retry(
        self, messages: Sequence[ChatMessage], schema: dict[str, Any], tier: ModelTier
    ) -> tuple[ProviderReply, int]:
        attempt = 0
        while True:
            attempt += 1
            try:
                async with asyncio.timeout(self._config.timeout_seconds):
                    reply = await self.provider.generate_structured(
                        messages=messages, schema=schema, tier=tier
                    )
                return reply, attempt
            except TimeoutError:
                error: AIProviderError = AIProviderTimeoutError("AI provider timed out")
            except AIProviderError as exc:
                error = exc
            logger.warning(
                "ai_provider_failed",
                provider=self.provider.name,
                reason=error.reason,
                attempt=attempt,
            )
            delay = self._retry_delay(error)
            if attempt > self._config.max_retries or delay is None:
                raise _CallFailed(error.reason, attempt)
            await self._sleep(delay)

    def _retry_delay(self, error: AIProviderError) -> float | None:
        """Seconds to wait before retrying, or None if this error isn't worth a retry."""
        if not error.retryable:
            return None
        if error.retry_after_seconds is not None:
            if error.retry_after_seconds > self._config.max_retry_after_seconds:
                return None
            return error.retry_after_seconds
        return self._config.retry_delay_seconds * (1 + self._jitter())


class _CallFailed(Exception):
    def __init__(self, reason: AIErrorReason, attempts: int) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.attempts = attempts


def _validate[T: BaseModel](data: object, output: type[T]) -> T | list[str]:
    """The validated model, or the validation problems without the offending values."""
    try:
        if isinstance(data, str):
            return output.model_validate_json(data)
        return output.model_validate(data)
    except ValidationError as exc:
        return [
            f"{'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]


def _repair_request(problems: list[str]) -> ChatMessage:
    listed = "\n".join(f"- {problem}" for problem in problems[:20])
    return ChatMessage(
        role="user",
        content=(
            "Your previous reply did not match the required JSON schema:\n"
            f"{listed}\n"
            "Reply again with a single JSON object that matches the schema exactly."
        ),
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
