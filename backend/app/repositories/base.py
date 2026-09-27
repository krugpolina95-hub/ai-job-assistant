"""Repository bases (architecture §4.1).

Repositories hold queries only. They never commit: the service that owns the use case owns
the transaction. Two kinds exist, so the authorization rule is structural, not a convention:

- `SharedRepository`: data no user owns (sources, vacancies, skills). Lookup by ID.
- `UserOwnedRepository`: every method takes `user_id` and filters by it. A row that belongs
  to someone else is indistinguishable from a missing row, so services answer 404 (AUTH-005).
"""

import uuid
from typing import Any, ClassVar

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute


class Repository[ModelT]:
    model: ClassVar[type[Any]]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, entity: ModelT) -> ModelT:
        self.session.add(entity)
        return entity

    async def flush(self) -> None:
        await self.session.flush()


class SharedRepository[ModelT](Repository[ModelT]):
    async def get(self, entity_id: uuid.UUID) -> ModelT | None:
        entity: ModelT | None = await self.session.get(self.model, entity_id)
        return entity


class UserOwnedRepository[ModelT](Repository[ModelT]):
    def _owned(self, user_id: uuid.UUID) -> Any:
        owner: InstrumentedAttribute[uuid.UUID] = self.model.user_id
        return owner == user_id

    async def get_for_user(self, user_id: uuid.UUID, entity_id: uuid.UUID) -> ModelT | None:
        statement = select(self.model).where(self.model.id == entity_id, self._owned(user_id))
        entity: ModelT | None = await self.session.scalar(statement)
        return entity

    async def delete_for_user(self, user_id: uuid.UUID, entity_id: uuid.UUID) -> bool:
        statement = (
            delete(self.model)
            .where(self.model.id == entity_id, self._owned(user_id))
            .returning(self.model.id)
        )
        deleted_id = await self.session.scalar(statement)
        return deleted_id is not None
