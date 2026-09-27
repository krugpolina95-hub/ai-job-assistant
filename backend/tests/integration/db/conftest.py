import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from tests.settings import DatabaseTestSettings

BACKEND_DIR = Path(__file__).resolve().parents[3]


def alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.attributes["database_url"] = database_url
    config.attributes["configure_logger"] = False
    return config


async def _reset_schema(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    url = DatabaseTestSettings().test_database_url
    if url is None:
        pytest.skip("TEST_DATABASE_URL is not set")
    return str(url)


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    """Builds the schema from zero with the real migrations, once per test session."""
    asyncio.run(_reset_schema(database_url))
    command.upgrade(alembic_config(database_url), "head")
    return database_url


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def db_engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    # NullPool: the migration round-trip test recreates extensions, which would leave pooled
    # asyncpg connections with stale type caches.
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back, so tests never leak data.

    Use `async with db_session.begin_nested():` around statements that are expected to fail.
    """
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
