from sqlalchemy import select

from app.models import Source
from app.repositories.base import SharedRepository


class SourceRepository(SharedRepository[Source]):
    model = Source

    async def get_by_key(self, adapter_key: str) -> Source | None:
        source: Source | None = await self.session.scalar(
            select(Source).where(Source.adapter_key == adapter_key)
        )
        return source
