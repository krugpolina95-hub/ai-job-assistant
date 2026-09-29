"""Saved search definitions (SEARCH-001..005, TD-10)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import SearchChannel, SearchStatus
from app.core.errors import NotFoundError
from app.models import Search
from app.repositories.search import SearchRepository
from app.schemas.search import PARAMS_SCHEMA_VERSION, SearchIn, SearchOut, SearchParamsV1


class SearchService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._searches = SearchRepository(db)

    async def create(self, user_id: uuid.UUID, data: SearchIn) -> SearchOut:
        """A form search arrives after the user reviewed and edited the parameters in the UI
        (SEARCH-004/005), so it is stored confirmed and can run. AI drafts come with M7."""
        search = self._searches.add(
            Search(
                user_id=user_id,
                title=data.title,
                normalized_params=data.params.model_dump(mode="json"),
                params_schema_version=PARAMS_SCHEMA_VERSION,
                channel=SearchChannel.FORM,
                status=SearchStatus.CONFIRMED,
            )
        )
        await self._db.commit()
        await self._db.refresh(search)
        return to_search_out(search)

    async def get(self, user_id: uuid.UUID, search_id: uuid.UUID) -> SearchOut:
        return to_search_out(await self.get_model(user_id, search_id))

    async def get_model(self, user_id: uuid.UUID, search_id: uuid.UUID) -> Search:
        search = await self._searches.get_for_user(user_id, search_id)
        if search is None:
            raise NotFoundError("Search not found")
        return search


def to_search_out(search: Search) -> SearchOut:
    return SearchOut(
        id=search.id,
        title=search.title,
        params=SearchParamsV1.model_validate(search.normalized_params),
        params_schema_version=search.params_schema_version,
        channel=search.channel,
        status=search.status,
        created_at=search.created_at,
        updated_at=search.updated_at,
    )
