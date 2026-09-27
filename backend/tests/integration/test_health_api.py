from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_readiness_checks
from app.core.config import Settings
from app.main import create_app
from app.services.readiness import ReadinessCheck


async def ok() -> None:
    return None


async def fails() -> None:
    raise ConnectionRefusedError


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def use_checks(app: FastAPI, checks: dict[str, ReadinessCheck]) -> None:
    app.dependency_overrides[get_readiness_checks] = lambda: checks


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_returns_200_when_dependencies_are_up(
    app: FastAPI, client: AsyncClient
) -> None:
    use_checks(app, {"database": ok, "redis": ok})

    response = await client.get("/api/v1/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


async def test_ready_returns_503_when_a_dependency_is_down(
    app: FastAPI, client: AsyncClient
) -> None:
    use_checks(app, {"database": fails, "redis": ok})

    response = await client.get("/api/v1/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "unavailable", "redis": "ok"},
    }


async def test_openapi_is_served_under_api_prefix_outside_production(client: AsyncClient) -> None:
    response = await client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    assert "/api/v1/health" in response.json()["paths"]
