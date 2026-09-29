import uuid
from datetime import datetime

from sqlalchemy import case, select, update

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

    async def record_failed_login(
        self, user_id: uuid.UUID, *, threshold: int, lock_until: datetime
    ) -> bool:
        """Counts one failed login, atomically. The failure that reaches `threshold` locks the
        account until `lock_until` and restarts the count. Returns True if it locked."""
        reached = User.failed_login_count + 1 >= threshold
        locked_until: datetime | None = await self.session.scalar(
            update(User)
            .where(User.id == user_id)
            .values(
                failed_login_count=case((reached, 0), else_=User.failed_login_count + 1),
                locked_until=case((reached, lock_until), else_=User.locked_until),
            )
            .returning(User.locked_until)
            .execution_options(synchronize_session=False)
        )
        return locked_until == lock_until
