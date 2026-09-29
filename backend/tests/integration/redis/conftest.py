from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.core.ids import uuid7
from tests.settings import DatabaseTestSettings


@pytest_asyncio.fixture(loop_scope="session")
async def redis() -> AsyncIterator[Redis]:
    url = DatabaseTestSettings().test_redis_url
    if url is None:
        pytest.skip("TEST_REDIS_URL is not set")
    client = Redis.from_url(str(url))
    yield client
    await client.aclose()


@pytest_asyncio.fixture(loop_scope="session")
async def key_prefix(redis: Redis) -> AsyncIterator[str]:
    """A unique namespace per test; its keys are deleted afterwards."""
    prefix = f"test:{uuid7().hex}"
    yield prefix
    keys = [key async for key in redis.scan_iter(match=f"{prefix}:*")]
    if keys:
        await redis.delete(*keys)
