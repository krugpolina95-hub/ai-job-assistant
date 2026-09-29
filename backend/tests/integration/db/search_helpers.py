"""Shared helpers for search-run tests."""

import uuid
from typing import Any

from httpx import AsyncClient, Response

from app.core.ids import uuid7
from app.integrations.sources.base import RateLimit


class RecordingDispatcher:
    """Records dispatched runs without executing them (tests of run creation)."""

    def __init__(self) -> None:
        self.dispatched: list[uuid.UUID] = []

    async def dispatch(self, job_id: uuid.UUID) -> None:
        self.dispatched.append(job_id)


class ScriptedLimiter:
    """Per-source rate limiter double: pops one scripted outcome (an exception to raise, or
    None to allow) per request; allows everything once the script is used up."""

    def __init__(self, script: dict[str, list[Exception | None]] | None = None) -> None:
        self.script = script or {}
        self.calls: list[tuple[str, float]] = []

    async def acquire(self, source_key: str, limit: RateLimit, *, max_wait: float) -> None:
        self.calls.append((source_key, max_wait))
        queue = self.script.get(source_key)
        if queue:
            outcome = queue.pop(0)
            if outcome is not None:
                raise outcome


async def sign_in(client: AsyncClient) -> uuid.UUID:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"user-{uuid7().hex}@example.com",
            "password": "correct horse battery",
            "accept_terms": True,
        },
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


def csrf(client: AsyncClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["csrf"]}


async def create_search(client: AsyncClient, **params: Any) -> str:
    body = {"params": {"roles": ["developer"]} | params}
    response = await client.post("/api/v1/searches", json=body, headers=csrf(client))
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def run(client: AsyncClient, search_id: str, key: str | None = None) -> Response:
    headers = csrf(client) | ({"Idempotency-Key": key} if key else {})
    return await client.post(f"/api/v1/searches/{search_id}/run", headers=headers)
