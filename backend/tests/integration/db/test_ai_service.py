"""AI entry point against the database: quotas, `ai_interactions` records, error mapping."""

import uuid
from typing import Any

import pytest
import structlog
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AIInteractionStatus, AITask
from app.core.errors import AIInvalidResponseError, AIUnavailableError, RateLimitedError
from app.integrations.ai.base import AIProviderUnavailableError, ChatMessage, ModelTier
from app.integrations.ai.client import AIClient, AIClientConfig, CircuitBreaker
from app.models import AIInteraction
from app.services.ai_service import AIService
from tests.conftest import build_test_settings
from tests.fakes import AllowAllLimiter, FakeLLMProvider, InMemoryRateLimiter
from tests.integration.db.factories import make_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

MESSAGES = [ChatMessage("user", "senior python developer in Vilnius")]
PROMPT_VERSION = "parse_query.v1"


class Parsed(BaseModel):
    roles: list[str]


async def no_wait(_seconds: float) -> None:
    return None


def service(
    db: AsyncSession, provider: FakeLLMProvider | None, *, limiter: Any = None, **settings: Any
) -> AIService:
    config = build_test_settings(**settings)
    client = AIClient(provider, AIClientConfig(), sleep=no_wait) if provider is not None else None
    return AIService(db, config, client, limiter or AllowAllLimiter())


async def call(ai: AIService, user_id: uuid.UUID) -> Parsed:
    return await ai.generate(
        user_id=user_id,
        task=AITask.PARSE_QUERY,
        prompt_version=PROMPT_VERSION,
        messages=MESSAGES,
        output=Parsed,
        tier=ModelTier.FAST,
    )


async def records(db: AsyncSession, user_id: uuid.UUID) -> list[AIInteraction]:
    return list(await db.scalars(select(AIInteraction).where(AIInteraction.user_id == user_id)))


async def test_success_returns_the_validated_model_and_records_metadata(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session)
    structlog.contextvars.bind_contextvars(request_id="req-123")
    try:
        result = await call(service(db_session, FakeLLMProvider({"roles": ["Developer"]})), user.id)
    finally:
        structlog.contextvars.unbind_contextvars("request_id")

    assert result == Parsed(roles=["Developer"])
    [row] = await records(db_session, user.id)
    assert (row.task, row.status, row.error_code) == (
        AITask.PARSE_QUERY,
        AIInteractionStatus.SUCCEEDED,
        None,
    )
    assert (row.provider, row.model, row.prompt_version) == ("fake", "fake-model-1", PROMPT_VERSION)
    assert (row.input_tokens, row.output_tokens, row.request_id) == (100, 20, "req-123")
    assert row.latency_ms is not None and row.latency_ms >= 0
    # Metadata only: nothing from the prompt or the reply is stored.
    stored = str(vars(row))
    assert "python developer" not in stored and "Developer" not in stored


async def test_unusable_reply_is_an_invalid_response_error(db_session: AsyncSession) -> None:
    user = await make_user(db_session)

    with pytest.raises(AIInvalidResponseError):
        await call(service(db_session, FakeLLMProvider("nope", {"roles": "x"})), user.id)

    [row] = await records(db_session, user.id)
    assert (row.status, row.error_code) == (AIInteractionStatus.FAILED, "invalid_response")
    assert (row.input_tokens, row.output_tokens) == (200, 40)


async def test_provider_outage_is_unavailable_and_recorded(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    down = FakeLLMProvider(AIProviderUnavailableError("HTTP 503"), AIProviderUnavailableError("x"))

    with pytest.raises(AIUnavailableError) as error:
        await call(service(db_session, down), user.id)

    assert error.value.status_code == 503
    [row] = await records(db_session, user.id)
    assert (row.status, row.error_code, row.model) == (
        AIInteractionStatus.FAILED,
        "provider_unavailable",
        "unknown",
    )


async def test_calls_that_never_reach_a_provider_are_not_recorded(
    db_session: AsyncSession,
) -> None:
    user = await make_user(db_session)
    provider = FakeLLMProvider()
    open_circuit = CircuitBreaker(threshold=1, open_seconds=60)
    open_circuit.record_failure()
    blocked = AIService(
        db_session,
        build_test_settings(),
        AIClient(provider, AIClientConfig(), breaker=open_circuit),
        AllowAllLimiter(),
    )

    with pytest.raises(AIUnavailableError):
        await call(blocked, user.id)
    with pytest.raises(AIUnavailableError):
        await call(service(db_session, None), user.id)  # no provider configured

    assert provider.calls == []
    assert await records(db_session, user.id) == []


async def test_daily_quota_stops_calls_before_the_provider(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    other = await make_user(db_session)
    provider = FakeLLMProvider(*[{"roles": ["Developer"]}] * 3)
    ai = service(db_session, provider, ai_max_calls_per_day=2)
    await call(ai, user.id)
    await call(ai, user.id)

    with pytest.raises(RateLimitedError) as error:
        await call(ai, user.id)

    assert error.value.details == {"limit": "ai_calls_per_day"}
    assert len(provider.calls) == 2
    assert await call(ai, other.id) == Parsed(roles=["Developer"])  # quotas are per user


async def test_failed_calls_count_toward_the_daily_quota(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    provider = FakeLLMProvider("bad", "bad", {"roles": ["Developer"]})
    ai = service(db_session, provider, ai_max_calls_per_day=1)
    with pytest.raises(AIInvalidResponseError):
        await call(ai, user.id)

    with pytest.raises(RateLimitedError):
        await call(ai, user.id)


async def test_per_minute_limit_answers_with_retry_after(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    provider = FakeLLMProvider(*[{"roles": ["Developer"]}] * 2)
    ai = service(db_session, provider, limiter=InMemoryRateLimiter(), ai_max_calls_per_minute=1)
    await call(ai, user.id)

    with pytest.raises(RateLimitedError) as error:
        await call(ai, user.id)

    assert error.value.details == {"limit": "ai_calls_per_minute"}
    assert error.value.headers is not None and int(error.value.headers["Retry-After"]) >= 1
    assert len(provider.calls) == 1
