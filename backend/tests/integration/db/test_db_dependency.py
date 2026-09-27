"""The per-request session dependency against a real database."""

from collections.abc import AsyncIterator

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.deps import DbSessionDep
from app.core.config import Settings
from app.core.ids import uuid7
from app.db.session import create_session_factory
from app.main import create_app
from app.models import Source

pytestmark = pytest.mark.asyncio(loop_scope="session")

router = APIRouter(prefix="/api/v1/_test")


@router.get("/db")
async def query(session: DbSessionDep) -> dict[str, int]:
    return {"value": await session.scalar(text("SELECT 41 + 1")) or 0}


@router.post("/uncommitted/{adapter_key}")
async def write_without_commit(adapter_key: str, session: DbSessionDep) -> dict[str, str]:
    session.add(
        Source(adapter_key=adapter_key, name="x", base_url="https://x.test", source_type="api")
    )
    await session.flush()
    return {"status": "flushed"}


@pytest.fixture
async def client(settings: Settings, db_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    app: FastAPI = create_app(settings)
    app.include_router(router)
    # ASGITransport does not run the lifespan; wire the session factory to the test database.
    app.state.db_session_factory = create_session_factory(db_engine)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_session_dependency_queries_the_database(client: AsyncClient) -> None:
    response = await client.get("/api/v1/_test/db")

    assert response.json() == {"value": 42}


async def test_uncommitted_work_is_rolled_back_after_the_request(
    client: AsyncClient, db_engine: AsyncEngine
) -> None:
    adapter_key = f"uncommitted-{uuid7().hex}"

    response = await client.post(f"/api/v1/_test/uncommitted/{adapter_key}")

    assert response.status_code == 200
    async with db_engine.connect() as connection:
        count = await connection.scalar(
            select(func.count()).select_from(Source).where(Source.adapter_key == adapter_key)
        )
    assert count == 0
