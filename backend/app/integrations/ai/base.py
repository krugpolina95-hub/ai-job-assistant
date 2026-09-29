"""AI provider port (TD-07, architecture §9).

`LLMProvider` is the only thing a concrete provider (e.g. an Anthropic SDK adapter, chosen by
the product owner) implements. It sends messages plus a JSON schema and returns the model's
structured reply as raw data. It never validates: `AIClient` does, so every provider gets the
same guardrails. Raw replies never leave `app/integrations/ai`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Literal, Protocol


class ModelTier(StrEnum):
    """Which class of model a task needs; each provider maps tiers to model IDs from config."""

    FAST = "fast"  # parse and clarify (TD-07)
    STANDARD = "standard"  # match explanations


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens
        )


@dataclass(frozen=True)
class ProviderReply:
    """What the provider returned for a structured request: parsed JSON when the provider's
    structured mode produced it, otherwise the text to parse. Unvalidated."""

    data: Mapping[str, Any] | str
    usage: TokenUsage
    model: str


class LLMProvider(Protocol):
    name: str

    async def generate_structured(
        self,
        *,
        messages: Sequence[ChatMessage],
        schema: Mapping[str, Any],
        tier: ModelTier,
    ) -> ProviderReply:
        """One call. Raises only `AIProviderError` subclasses, with messages that never
        contain the prompt, the reply or credentials."""
        ...


class AIErrorReason(StrEnum):
    """Why an AI call failed. Stored in `ai_interactions.error_code` and logged; the API
    only exposes AI_UNAVAILABLE or AI_INVALID_RESPONSE (app/core/errors.py)."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_REJECTED = "provider_rejected"
    CIRCUIT_OPEN = "circuit_open"
    INVALID_RESPONSE = "invalid_response"


class AIProviderError(Exception):
    reason: ClassVar[AIErrorReason]
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after_seconds = retry_after_seconds


class AIProviderUnavailableError(AIProviderError):
    """Network failure or a 5xx from the provider."""

    reason = AIErrorReason.PROVIDER_UNAVAILABLE
    retryable = True


class AIProviderRateLimitedError(AIProviderError):
    """The provider answered 429 (our account's limit, not the user's quota)."""

    reason = AIErrorReason.PROVIDER_RATE_LIMITED
    retryable = True


class AIProviderTimeoutError(AIProviderError):
    reason = AIErrorReason.PROVIDER_TIMEOUT


class AIProviderRejectedError(AIProviderError):
    """A 4xx other than 429 (bad request, auth, content policy): retrying won't help."""

    reason = AIErrorReason.PROVIDER_REJECTED
