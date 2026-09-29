"""The one entry point for AI calls from backend services (TD-07, architecture §9).

Feature services (query parsing M7, match explanations M9) build the messages and the output
schema; this service adds what every AI call needs: per-user quotas, the `ai_interactions`
record (metadata only, never prompt or reply text), and mapping failures to API errors. Callers
get the validated model or an `AppError`, never a raw provider reply.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import structlog
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import AIInteractionStatus, AITask
from app.core.errors import AIInvalidResponseError, AIUnavailableError, RateLimitedError
from app.core.rate_limit import RateLimiter, RateLimitRule
from app.integrations.ai.base import AIErrorReason, ChatMessage, ModelTier, TokenUsage
from app.integrations.ai.client import AICallError, AIClient
from app.models import AIInteraction
from app.repositories.ai import AIInteractionRepository

logger = structlog.stdlib.get_logger(__name__)

UNKNOWN_MODEL = "unknown"


class AIService:
    def __init__(
        self,
        db: AsyncSession,
        settings: Settings,
        client: AIClient | None,
        limiter: RateLimiter,
    ) -> None:
        self._db = db
        self._settings = settings
        self._client = client
        self._limiter = limiter
        self._interactions = AIInteractionRepository(db)
        self._per_minute = RateLimitRule(
            "ai_calls_per_minute", settings.ai_max_calls_per_minute, timedelta(minutes=1)
        )

    async def generate[T: BaseModel](
        self,
        *,
        user_id: uuid.UUID,
        task: AITask,
        prompt_version: str,
        messages: Sequence[ChatMessage],
        output: type[T],
        tier: ModelTier,
        search_id: uuid.UUID | None = None,
    ) -> T:
        client = self._client
        if client is None:
            logger.warning("ai_not_configured", task=task)
            raise AIUnavailableError()
        await self._enforce_quotas(user_id)

        try:
            result = await client.generate(messages, output, tier)
        except AICallError as failure:
            if failure.provider_calls:  # only calls that reached the provider cost anything
                self._record(
                    client.provider.name,
                    user_id,
                    task,
                    prompt_version,
                    search_id,
                    status=AIInteractionStatus.FAILED,
                    error=failure.reason,
                    model=failure.model,
                    usage=failure.usage,
                    latency_ms=failure.latency_ms,
                )
                await self._db.commit()
            logger.warning("ai_call_failed", task=task, reason=failure.reason)
            if failure.reason is AIErrorReason.INVALID_RESPONSE:
                raise AIInvalidResponseError() from None
            raise AIUnavailableError() from None

        self._record(
            client.provider.name,
            user_id,
            task,
            prompt_version,
            search_id,
            status=AIInteractionStatus.SUCCEEDED,
            error=None,
            model=result.model,
            usage=result.usage,
            latency_ms=result.latency_ms,
        )
        await self._db.commit()
        logger.info("ai_call_succeeded", task=task, provider_calls=result.provider_calls)
        return result.value

    async def _enforce_quotas(self, user_id: uuid.UUID) -> None:
        """TD-07 daily quota (rolling 24 h, counted from `ai_interactions`, so it survives a
        Redis loss) and the per-minute limit from architecture §4.4 (Redis)."""
        day_ago = datetime.now(UTC) - timedelta(days=1)
        if await self._interactions.count_since_for_user(user_id, day_ago) >= (
            self._settings.ai_max_calls_per_day
        ):
            raise RateLimitedError(details={"limit": "ai_calls_per_day"})
        decision = await self._limiter.hit(self._per_minute, str(user_id))
        if not decision.allowed:
            raise RateLimitedError(
                details={"limit": self._per_minute.name},
                retry_after_seconds=decision.retry_after_seconds,
            )

    def _record(
        self,
        provider: str,
        user_id: uuid.UUID,
        task: AITask,
        prompt_version: str,
        search_id: uuid.UUID | None,
        *,
        status: AIInteractionStatus,
        error: AIErrorReason | None,
        model: str | None,
        usage: TokenUsage,
        latency_ms: int,
    ) -> None:
        self._interactions.add(
            AIInteraction(
                user_id=user_id,
                search_id=search_id,
                task=task,
                provider=provider,
                model=model or UNKNOWN_MODEL,
                prompt_version=prompt_version,
                request_id=structlog.contextvars.get_contextvars().get("request_id"),
                status=status,
                error_code=error.value if error else None,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=latency_ms,
            )
        )
