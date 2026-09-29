"""Brute-force protection and the security event log for the auth services (AUTH-006).

Request limits follow architecture §4.4. They are keyed by client IP, email or user and
live in Redis. The per-account lockout lives in the database (`users.locked_until`), so it
holds even if Redis is lost.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import AuthEventType
from app.core.errors import RateLimitedError
from app.core.network import ip_prefix
from app.core.rate_limit import RateLimiter, RateLimitRule
from app.core.security import hash_identifier
from app.models import AuthEvent
from app.repositories.auth import AuthEventRepository

logger = structlog.stdlib.get_logger(__name__)

USER_AGENT_MAX_LENGTH = 512

# Failed logins count; successful ones don't.
LOGIN_PER_IP_AND_EMAIL = RateLimitRule("login_per_ip_and_email", 5, timedelta(minutes=1))
LOGIN_PER_IP = RateLimitRule("login_per_ip", 20, timedelta(hours=1))
# Every attempt counts.
REGISTER_PER_IP = RateLimitRule("register_per_ip", 5, timedelta(hours=1))
RESET_REQUEST_PER_IP = RateLimitRule("password_reset_request_per_ip", 5, timedelta(hours=1))
# Keeps one mailbox from being flooded with reset emails from many IPs.
RESET_REQUEST_PER_EMAIL = RateLimitRule("password_reset_request_per_email", 3, timedelta(hours=1))
RESET_PER_IP = RateLimitRule("password_reset_per_ip", 10, timedelta(hours=1))
PASSWORD_CHANGE_PER_USER = RateLimitRule("password_change_per_user", 5, timedelta(minutes=15))


@dataclass(frozen=True)
class ClientInfo:
    """The caller as far as security logging needs it. `ip` is used for limits and stored
    only as a network prefix."""

    ip: str | None
    user_agent: str | None

    @property
    def ip_key(self) -> str:
        return self.ip or "unknown"


def normalize_email(email: str) -> str:
    return email.strip().casefold()


class AuthGuard:
    def __init__(self, db: AsyncSession, settings: Settings, limiter: RateLimiter) -> None:
        self._db = db
        self._settings = settings
        self._limiter = limiter
        self._events = AuthEventRepository(db)

    def record(
        self,
        event_type: AuthEventType,
        client: ClientInfo,
        *,
        user_id: uuid.UUID | None = None,
        email: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Adds an event to the current transaction; the caller commits."""
        self._events.add(
            AuthEvent(
                user_id=user_id,
                event_type=event_type,
                email_hash=self.email_hash(email) if email else None,
                ip_prefix=ip_prefix(client.ip),
                user_agent=client.user_agent[:USER_AGENT_MAX_LENGTH] if client.user_agent else None,
                detail=detail,
            )
        )

    async def check(
        self, rule: RateLimitRule, key: str, client: ClientInfo, *, email: str | None = None
    ) -> None:
        """Refuses the request if `rule` is exhausted, without counting it."""
        decision = await self._limiter.check(rule, key)
        if not decision.allowed:
            await self._refuse(rule, decision.retry_after_seconds, client, email)

    async def hit(
        self,
        rule: RateLimitRule,
        key: str,
        client: ClientInfo,
        *,
        email: str | None = None,
        user_id: uuid.UUID | None = None,
    ) -> None:
        """Counts one attempt, or refuses the request if `rule` is exhausted."""
        decision = await self._limiter.hit(rule, key)
        if not decision.allowed:
            await self._refuse(rule, decision.retry_after_seconds, client, email, user_id)

    async def count_failure(self, rule: RateLimitRule, key: str) -> None:
        """Counts a failed attempt after the fact (e.g. a wrong password). The refusal, if
        any, applies to the next attempt."""
        await self._limiter.hit(rule, key)

    def email_hash(self, email: str) -> str:
        return hash_identifier(
            normalize_email(email), self._settings.log_hash_secret.get_secret_value()
        )

    async def _refuse(
        self,
        rule: RateLimitRule,
        retry_after_seconds: int,
        client: ClientInfo,
        email: str | None,
        user_id: uuid.UUID | None = None,
    ) -> None:
        self.record(
            AuthEventType.RATE_LIMITED, client, user_id=user_id, email=email, detail=rule.name
        )
        await self._db.commit()
        logger.warning("auth_rate_limited", rule=rule.name)
        raise RateLimitedError(
            details={"limit": rule.name}, retry_after_seconds=retry_after_seconds
        )
