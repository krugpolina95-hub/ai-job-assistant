"""Registration, login, logout and session authentication (AUTH-001..004, TD-03, TD-04)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    UnauthenticatedError,
)
from app.core.security import PasswordHasher, generate_token, hash_identifier, hash_token
from app.db.errors import violated_constraint
from app.models import User, UserSession
from app.repositories.session import UserSessionRepository
from app.repositories.user import UserRepository
from app.schemas.auth import RegisterIn

logger = structlog.stdlib.get_logger(__name__)

USER_AGENT_MAX_LENGTH = 512


@dataclass(frozen=True)
class IssuedSession:
    """A new session. `token` is the only copy of the secret: it goes into the cookie."""

    token: str
    expires_at: datetime
    persistent: bool


@dataclass(frozen=True)
class AuthResult:
    user: User
    session: IssuedSession


@dataclass(frozen=True)
class AuthContext:
    user: User
    session_id: uuid.UUID


class AuthService:
    def __init__(self, db: AsyncSession, hasher: PasswordHasher, settings: Settings) -> None:
        self._db = db
        self._hasher = hasher
        self._settings = settings
        self._users = UserRepository(db)
        self._sessions = UserSessionRepository(db)

    async def register(self, data: RegisterIn, *, user_agent: str | None) -> AuthResult:
        email = str(data.email)
        if await self._users.get_by_email(email) is not None:
            raise EmailAlreadyRegisteredError()

        now = _now()
        user = self._users.add(
            User(
                email=email,
                password_hash=await self._hasher.hash(data.password.get_secret_value()),
                preferred_locale=data.preferred_locale.value,
                timezone=data.timezone,
                terms_version=self._settings.legal_terms_version,
                privacy_version=self._settings.legal_privacy_version,
                terms_accepted_at=now,
                last_login_at=now,
            )
        )
        try:
            await self._users.flush()
        except IntegrityError as exc:
            # A concurrent registration with the same email won the race.
            if violated_constraint(exc) == "uq_users_email":
                raise EmailAlreadyRegisteredError() from exc
            raise

        issued = self._issue_session(user, persistent=False, user_agent=user_agent, now=now)
        await self._db.commit()
        logger.info("user_registered", user=self._log_id(user.id))
        return AuthResult(user=user, session=issued)

    async def login(
        self,
        email: str,
        password: str,
        *,
        remember_me: bool,
        user_agent: str | None,
        previous_token: str | None,
    ) -> AuthResult:
        user = await self._users.get_by_email(email)
        if user is None:
            await self._hasher.verify_dummy(password)
            logger.info("login_failed", reason="unknown_account")
            raise InvalidCredentialsError()

        valid, upgraded_hash = await self._hasher.verify(password, user.password_hash)
        if not valid:
            logger.info("login_failed", reason="wrong_password", user=self._log_id(user.id))
            raise InvalidCredentialsError()
        if upgraded_hash is not None:
            user.password_hash = upgraded_hash

        now = _now()
        # New token on every login (session fixation); the browser's old session ends here.
        if previous_token:
            await self._sessions.delete_by_token_hash(hash_token(previous_token))
        await self._sessions.delete_expired_for_user(user.id, now)
        user.last_login_at = now
        issued = self._issue_session(user, persistent=remember_me, user_agent=user_agent, now=now)
        await self._db.commit()
        logger.info("login_succeeded", user=self._log_id(user.id), rehashed=bool(upgraded_hash))
        return AuthResult(user=user, session=issued)

    async def authenticate(self, token: str | None) -> AuthContext:
        if not token:
            raise UnauthenticatedError()
        user_session = await self._sessions.get_by_token_hash(hash_token(token))
        if user_session is None:
            raise UnauthenticatedError()

        now = _now()
        idle_deadline = user_session.last_seen_at + self._settings.session_idle_timeout
        if user_session.expires_at <= now or idle_deadline <= now:
            await self._sessions.delete_for_user(user_session.user_id, user_session.id)
            await self._db.commit()
            raise UnauthenticatedError()

        # Sliding idle expiry, written at most once per interval to limit write load.
        if now - user_session.last_seen_at >= self._settings.session_touch_interval:
            user_session.last_seen_at = now
            await self._db.commit()
        return AuthContext(user=user_session.user, session_id=user_session.id)

    async def logout(self, auth: AuthContext) -> None:
        await self._sessions.delete_for_user(auth.user.id, auth.session_id)
        await self._db.commit()
        logger.info("logout", user=self._log_id(auth.user.id))

    def _issue_session(
        self, user: User, *, persistent: bool, user_agent: str | None, now: datetime
    ) -> IssuedSession:
        lifetime = (
            self._settings.session_absolute_timeout
            if persistent
            else self._settings.session_browser_timeout
        )
        token = generate_token()
        user_session = UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            last_seen_at=now,
            expires_at=now + lifetime,
            user_agent=user_agent[:USER_AGENT_MAX_LENGTH] if user_agent else None,
        )
        self._sessions.add(user_session)
        return IssuedSession(token=token, expires_at=user_session.expires_at, persistent=persistent)

    def _log_id(self, user_id: uuid.UUID) -> str:
        return hash_identifier(str(user_id), self._settings.log_hash_secret.get_secret_value())


def _now() -> datetime:
    return datetime.now(UTC)
