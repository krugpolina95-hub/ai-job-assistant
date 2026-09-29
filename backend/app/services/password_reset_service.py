"""Password reset by email (AUTH-004, requirements-analysis F2, TD-03, TD-27).

A request always gets the same answer, whether or not the email has an account, and the email
goes out in the background, so neither the response nor its timing reveals an account. The
emailed token is single-use, expires after PASSWORD_RESET_TOKEN_TTL and is stored only as its
SHA-256. A reset signs the user out everywhere and lifts a login lockout.
"""

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import AuthEventType
from app.core.errors import ResetTokenInvalidError
from app.core.rate_limit import RateLimiter
from app.core.security import PasswordHasher, generate_token, hash_identifier, hash_token
from app.integrations.email.base import EmailSender, OutgoingEmail
from app.integrations.email.templates import password_reset_email
from app.models import PasswordResetToken
from app.repositories.auth import PasswordResetTokenRepository
from app.repositories.session import UserSessionRepository
from app.repositories.user import UserRepository
from app.services.auth_guard import (
    RESET_PER_IP,
    RESET_REQUEST_PER_EMAIL,
    RESET_REQUEST_PER_IP,
    AuthGuard,
    ClientInfo,
    normalize_email,
)
from app.services.auth_service import password_equals_email_error, password_matches_email

logger = structlog.stdlib.get_logger(__name__)

RESET_PATH = "reset-password"


class PasswordResetService:
    def __init__(
        self,
        db: AsyncSession,
        hasher: PasswordHasher,
        settings: Settings,
        limiter: RateLimiter,
        email_sender: EmailSender,
    ) -> None:
        self._db = db
        self._hasher = hasher
        self._settings = settings
        self._email_sender = email_sender
        self._users = UserRepository(db)
        self._sessions = UserSessionRepository(db)
        self._tokens = PasswordResetTokenRepository(db)
        self._guard = AuthGuard(db, settings, limiter)

    async def request_reset(self, email: str, client: ClientInfo) -> None:
        await self._guard.hit(RESET_REQUEST_PER_IP, client.ip_key, client, email=email)
        await self._guard.hit(RESET_REQUEST_PER_EMAIL, normalize_email(email), client, email=email)

        user = await self._users.get_by_email(email)
        if user is None:
            self._guard.record(AuthEventType.PASSWORD_RESET_REQUESTED, client, email=email)
            await self._db.commit()
            logger.info("password_reset_requested", known_account=False)
            return

        now = _now()
        token = generate_token()
        # Only the newest link works.
        await self._tokens.delete_unused_for_user(user.id)
        self._tokens.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(token),
                expires_at=now + self._settings.password_reset_token_ttl,
            )
        )
        self._guard.record(
            AuthEventType.PASSWORD_RESET_REQUESTED, client, user_id=user.id, email=email
        )
        await self._db.commit()

        subject, body = password_reset_email(
            user.preferred_locale,
            link=self._reset_link(user.preferred_locale, token),
            minutes=int(self._settings.password_reset_token_ttl.total_seconds() // 60),
        )
        await self._email_sender.send(OutgoingEmail(to=user.email, subject=subject, body=body))
        logger.info("password_reset_requested", known_account=True, user=self._log_id(user.id))

    async def reset_password(self, token: str, new_password: str, client: ClientInfo) -> None:
        await self._guard.hit(RESET_PER_IP, client.ip_key, client)

        user_id = await self._tokens.claim(hash_token(token), _now())
        user = await self._users.get(user_id) if user_id else None
        if user is None:
            logger.info("password_reset_rejected")
            raise ResetTokenInvalidError()
        if password_matches_email(new_password, user.email):
            # The token stays unused: the rollback undoes the claim.
            await self._db.rollback()
            raise password_equals_email_error("new_password")

        user.password_hash = await self._hasher.hash(new_password)
        user.failed_login_count = 0
        user.locked_until = None
        await self._tokens.delete_unused_for_user(user.id)
        await self._sessions.delete_all_for_user(user.id)
        self._guard.record(AuthEventType.PASSWORD_RESET, client, user_id=user.id)
        await self._db.commit()
        logger.info("password_reset", user=self._log_id(user.id))

    def _reset_link(self, locale: str, token: str) -> str:
        # The token goes in the fragment: browsers never send it to servers or in Referer.
        return f"{self._settings.public_origin}/{locale}/{RESET_PATH}#token={token}"

    def _log_id(self, user_id: uuid.UUID) -> str:
        return hash_identifier(str(user_id), self._settings.log_hash_secret.get_secret_value())


def _now() -> datetime:
    return datetime.now(UTC)
