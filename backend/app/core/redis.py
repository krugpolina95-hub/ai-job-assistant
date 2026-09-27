from redis.asyncio import Redis

from app.core.config import Settings


def create_redis(settings: Settings) -> Redis:
    return Redis.from_url(str(settings.redis_url))


async def ping_redis(client: Redis) -> None:
    await client.ping()
