import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import joinedload

from app.models import UserSession
from app.repositories.base import UserOwnedRepository


class UserSessionRepository(UserOwnedRepository[UserSession]):
    model = UserSession

    async def get_by_token_hash(self, token_hash: str) -> UserSession | None:
        """The one lookup without `user_id`: it is how a request finds its user."""
        statement = (
            select(UserSession)
            .options(joinedload(UserSession.user))
            .where(UserSession.token_hash == token_hash)
        )
        user_session: UserSession | None = await self.session.scalar(statement)
        return user_session

    async def delete_by_token_hash(self, token_hash: str) -> None:
        await self.session.execute(delete(UserSession).where(UserSession.token_hash == token_hash))

    async def delete_expired_for_user(self, user_id: uuid.UUID, now: datetime) -> None:
        await self.session.execute(
            delete(UserSession).where(UserSession.user_id == user_id, UserSession.expires_at <= now)
        )

    async def delete_all_for_user(self, user_id: uuid.UUID) -> None:
        """Signs the user out everywhere (password change or reset, TD-03)."""
        await self.session.execute(delete(UserSession).where(UserSession.user_id == user_id))
