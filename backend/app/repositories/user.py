import uuid

from sqlalchemy import select

from app.models import User
from app.repositories.base import Repository


class UserRepository(Repository[User]):
    model = User

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        # `email` is citext, so this comparison is case-insensitive.
        user: User | None = await self.session.scalar(select(User).where(User.email == email))
        return user
