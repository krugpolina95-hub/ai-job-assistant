import uuid
from datetime import datetime

from sqlalchemy import delete, update

from app.models import AuthEvent, PasswordResetToken
from app.repositories.base import Repository, UserOwnedRepository


class AuthEventRepository(Repository[AuthEvent]):
    """Append-only security log. Events may have no user (an unknown email), so it isn't a
    user-owned repository; nothing in the API reads it."""

    model = AuthEvent


class PasswordResetTokenRepository(UserOwnedRepository[PasswordResetToken]):
    model = PasswordResetToken

    async def claim(self, token_hash: str, now: datetime) -> uuid.UUID | None:
        """The one lookup without `user_id`: the emailed token is how a reset finds its user.
        Marks a live token used and returns its user, atomically, so a token works once even
        under concurrent requests."""
        user_id: uuid.UUID | None = await self.session.scalar(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.token_hash == token_hash,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > now,
            )
            .values(used_at=now)
            .returning(PasswordResetToken.user_id)
            .execution_options(synchronize_session=False)
        )
        return user_id

    async def delete_unused_for_user(self, user_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(PasswordResetToken).where(
                PasswordResetToken.user_id == user_id, PasswordResetToken.used_at.is_(None)
            )
        )
