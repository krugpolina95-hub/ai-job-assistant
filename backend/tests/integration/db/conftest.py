import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401  (all tables on Base.metadata)
from app.core.config import Settings
from app.db.base import Base
from app.main import create_app
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
async def db_connection(db_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """One connection per test inside a transaction that is always rolled back, so tests
    never leak data, even through code that commits (commits become savepoint releases)."""
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            await transaction.rollback()


@pytest_asyncio.fixture(loop_scope="session")
async def db_session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """Use `async with db_session.begin_nested():` around statements expected to fail."""
    session = AsyncSession(
        bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        yield session
    finally:
        await session.close()


@pytest.fixture
def api_app(settings: Settings, db_connection: AsyncConnection) -> FastAPI:
    """The real app, with request sessions bound to the test transaction."""
    app = create_app(settings)
    app.state.db_session_factory = async_sessionmaker(
        bind=db_connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
        autoflush=False,
    )
    return app


@pytest_asyncio.fixture(loop_scope="session")
async def api_client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    # https: session cookies are Secure, and the client only sends them over https.
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client


@pytest_asyncio.fixture(loop_scope="session")
async def committed_sessions(
    db_engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, each on its own connection. For code that must run in
    parallel (search-run workers, concurrent requests), which a single rolled-back connection
    can't do. Every table is emptied afterwards."""
    yield async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    async with db_engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
