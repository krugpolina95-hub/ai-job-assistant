"""Registration, login, logout, password change and session authentication (AUTH-001..006,
TD-03, TD-04)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import AuthEventType
from app.core.errors import (
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidCurrentPasswordError,
    UnauthenticatedError,
    ValidationFailedError,
)
from app.core.rate_limit import RateLimiter
from app.core.security import PasswordHasher, generate_token, hash_identifier, hash_token
from app.db.errors import violated_constraint
from app.models import User, UserSession
from app.repositories.session import UserSessionRepository
from app.repositories.user import UserRepository
from app.schemas.auth import RegisterIn
from app.services.auth_guard import (
    LOGIN_PER_IP,
    LOGIN_PER_IP_AND_EMAIL,
    PASSWORD_CHANGE_PER_USER,
    REGISTER_PER_IP,
    USER_AGENT_MAX_LENGTH,
    AuthGuard,
    ClientInfo,
    normalize_email,
)

logger = structlog.stdlib.get_logger(__name__)


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
    persistent: bool  # signed in with "remember me"


def password_matches_email(password: str, email: str) -> bool:
    """TD-04: the password must not be the email address itself."""
    return password.casefold() == normalize_email(email)


def password_equals_email_error(field: str) -> ValidationFailedError:
    """Same shape as request validation errors, so clients show it next to the field."""
    return ValidationFailedError(
        details=[
            {
                "loc": ["body", field],
                "type": "value_error",
                "message": "Password must not be the email address",
            }
        ]
    )


class AuthService:
    def __init__(
        self,
        db: AsyncSession,
        hasher: PasswordHasher,
        settings: Settings,
        limiter: RateLimiter,
    ) -> None:
        self._db = db
        self._hasher = hasher
        self._settings = settings
        self._users = UserRepository(db)
        self._sessions = UserSessionRepository(db)
        self._guard = AuthGuard(db, settings, limiter)

    async def register(self, data: RegisterIn, client: ClientInfo) -> AuthResult:
        await self._guard.hit(REGISTER_PER_IP, client.ip_key, client)
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

        issued = self._issue_session(user, persistent=False, client=client, now=now)
        await self._db.commit()
        logger.info("user_registered", user=self._log_id(user.id))
        return AuthResult(user=user, session=issued)

    async def login(
        self,
        email: str,
        password: str,
        *,
        remember_me: bool,
        client: ClientInfo,
        previous_token: str | None,
    ) -> AuthResult:
        ip_and_email = f"{client.ip_key}|{normalize_email(email)}"
        await self._guard.check(LOGIN_PER_IP_AND_EMAIL, ip_and_email, client, email=email)
        await self._guard.check(LOGIN_PER_IP, client.ip_key, client, email=email)

        user = await self._users.get_by_email(email)
        now = _now()
        if user is None:
            await self._hasher.verify_dummy(password)
            await self._fail_login(client, email, ip_and_email, None, "unknown_account")

        valid, upgraded_hash = await self._hasher.verify(password, user.password_hash)
        if user.locked_until is not None and user.locked_until > now:
            # Even the right password is refused while locked, with the usual answer, so a
            # lock reveals nothing. A password reset lifts it.
            await self._fail_login(client, email, ip_and_email, user.id, "account_locked")
        if not valid:
            locked = await self._users.record_failed_login(
                user.id,
                threshold=self._settings.login_lockout_threshold,
                lock_until=now + self._settings.login_lockout_duration,
            )
            if locked:
                self._guard.record(
                    AuthEventType.ACCOUNT_LOCKED, client, user_id=user.id, email=email
                )
                logger.warning("account_locked", user=self._log_id(user.id))
            await self._fail_login(client, email, ip_and_email, user.id, "wrong_password")

        if upgraded_hash is not None:
            user.password_hash = upgraded_hash
        user.failed_login_count = 0
        user.locked_until = None
        # New token on every login (session fixation); the browser's old session ends here.
        if previous_token:
            await self._sessions.delete_by_token_hash(hash_token(previous_token))
        await self._sessions.delete_expired_for_user(user.id, now)
        user.last_login_at = now
        issued = self._issue_session(user, persistent=remember_me, client=client, now=now)
        self._guard.record(AuthEventType.LOGIN_SUCCEEDED, client, user_id=user.id, email=email)
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
        lifetime = user_session.expires_at - user_session.created_at
        return AuthContext(
            user=user_session.user,
            session_id=user_session.id,
            persistent=lifetime > self._settings.session_browser_timeout,
        )

    async def logout(self, auth: AuthContext) -> None:
        await self._sessions.delete_for_user(auth.user.id, auth.session_id)
        await self._db.commit()
        logger.info("logout", user=self._log_id(auth.user.id))

    async def change_password(
        self,
        auth: AuthContext,
        current_password: str,
        new_password: str,
        client: ClientInfo,
    ) -> IssuedSession:
        """Replaces the password and every session, the current one included: whoever holds
        an old token, even a copy of this browser's, is signed out (TD-03). The caller gets a
        fresh session."""
        user = auth.user
        await self._guard.hit(PASSWORD_CHANGE_PER_USER, str(user.id), client, user_id=user.id)
        valid, _ = await self._hasher.verify(current_password, user.password_hash)
        if not valid:
            self._guard.record(
                AuthEventType.LOGIN_FAILED, client, user_id=user.id, detail="password_change"
            )
            await self._db.commit()
            logger.info("password_change_failed", user=self._log_id(user.id))
            raise InvalidCurrentPasswordError()
        if password_matches_email(new_password, user.email):
            raise password_equals_email_error("new_password")

        now = _now()
        user.password_hash = await self._hasher.hash(new_password)
        await self._sessions.delete_all_for_user(user.id)
        issued = self._issue_session(user, persistent=auth.persistent, client=client, now=now)
        self._guard.record(AuthEventType.PASSWORD_CHANGED, client, user_id=user.id)
        await self._db.commit()
        logger.info("password_changed", user=self._log_id(user.id))
        return issued

    async def _fail_login(
        self,
        client: ClientInfo,
        email: str,
        ip_and_email: str,
        user_id: uuid.UUID | None,
        reason: str,
    ) -> NoReturn:
        await self._guard.count_failure(LOGIN_PER_IP_AND_EMAIL, ip_and_email)
        await self._guard.count_failure(LOGIN_PER_IP, client.ip_key)
        self._guard.record(
            AuthEventType.LOGIN_FAILED, client, user_id=user_id, email=email, detail=reason
        )
        await self._db.commit()
        logger.info(
            "login_failed",
            reason=reason,
            **({"user": self._log_id(user_id)} if user_id else {}),
        )
        raise InvalidCredentialsError()

    def _issue_session(
        self, user: User, *, persistent: bool, client: ClientInfo, now: datetime
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
            # Set here, not by the database, so expires_at - created_at is exactly the
            # lifetime: that is how a session is later known to be persistent.
            created_at=now,
            last_seen_at=now,
            expires_at=now + lifetime,
            user_agent=client.user_agent[:USER_AGENT_MAX_LENGTH] if client.user_agent else None,
        )
        self._sessions.add(user_session)
        return IssuedSession(token=token, expires_at=user_session.expires_at, persistent=persistent)

    def _log_id(self, user_id: uuid.UUID) -> str:
        return hash_identifier(str(user_id), self._settings.log_hash_secret.get_secret_value())


def _now() -> datetime:
    return datetime.now(UTC)
