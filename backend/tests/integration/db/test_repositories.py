import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import uuid7
from app.models import Search, Source
from app.repositories.base import SharedRepository, UserOwnedRepository
from tests.integration.db.factories import make_search, make_source, make_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


class SearchRepository(UserOwnedRepository[Search]):
    model = Search


class SourceRepository(SharedRepository[Source]):
    model = Source


async def test_user_owned_get_returns_only_the_owners_row(db_session: AsyncSession) -> None:
    owner = await make_user(db_session)
    stranger = await make_user(db_session)
    search = await make_search(db_session, owner)
    repository = SearchRepository(db_session)

    assert await repository.get_for_user(owner.id, search.id) is search
    assert await repository.get_for_user(stranger.id, search.id) is None
    assert await repository.get_for_user(owner.id, uuid7()) is None


async def test_user_owned_delete_ignores_other_users_rows(db_session: AsyncSession) -> None:
    owner = await make_user(db_session)
    stranger = await make_user(db_session)
    search = await make_search(db_session, owner)
    repository = SearchRepository(db_session)

    assert await repository.delete_for_user(stranger.id, search.id) is False
    assert await repository.get_for_user(owner.id, search.id) is not None

    assert await repository.delete_for_user(owner.id, search.id) is True
    db_session.expunge_all()
    assert await repository.get_for_user(owner.id, search.id) is None


async def test_shared_repository_add_and_get(db_session: AsyncSession) -> None:
    repository = SourceRepository(db_session)
    source = repository.add(
        Source(
            adapter_key=f"mock-{uuid7().hex}",
            name="Mock",
            base_url="https://jobs.example.com",
            source_type="api",
        )
    )
    await repository.flush()

    assert await repository.get(source.id) is source
    assert await repository.get(uuid7()) is None
    assert await repository.get((await make_source(db_session)).id) is not None
