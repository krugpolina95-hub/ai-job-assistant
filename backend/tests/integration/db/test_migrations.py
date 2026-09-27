from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

import app.models  # noqa: F401
from app.db.base import Base
from tests.integration.db.conftest import alembic_config


def _schema_diff(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(connection, opts={"compare_type": True})
    return list(compare_metadata(context, Base.metadata))


def test_migrations_downgrade_to_base_and_upgrade_again(migrated_database: str) -> None:
    config = alembic_config(migrated_database)

    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest.mark.asyncio(loop_scope="session")
async def test_models_match_migrations(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        diff = await connection.run_sync(_schema_diff)

    assert diff == [], "models and migrations disagree; generate a new migration"


@pytest.mark.asyncio(loop_scope="session")
async def test_required_extensions_are_installed(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as connection:
        result = await connection.execute(
            text("SELECT extname FROM pg_extension WHERE extname IN ('citext', 'pg_trgm')")
        )

    assert set(result.scalars()) == {"citext", "pg_trgm"}
