import uuid
from datetime import datetime

from sqlalchemy import func, select

from app.models import AIInteraction
from app.repositories.base import UserOwnedRepository


class AIInteractionRepository(UserOwnedRepository[AIInteraction]):
    model = AIInteraction

    async def count_since_for_user(self, user_id: uuid.UUID, since: datetime) -> int:
        count = await self.session.scalar(
            select(func.count())
            .select_from(AIInteraction)
            .where(AIInteraction.user_id == user_id, AIInteraction.created_at >= since)
        )
        return count or 0
