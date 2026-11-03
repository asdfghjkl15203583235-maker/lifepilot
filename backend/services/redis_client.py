"""Redis connection lifecycle — mirrors db/session.py pattern."""

import logging
from dataclasses import dataclass

import redis.asyncio as aioredis

from config import Settings

logger = logging.getLogger(__name__)


@dataclass
class _RedisHolder:
    client: aioredis.Redis | None = None


_holder = _RedisHolder()


async def init_redis(settings: Settings) -> None:
    """Create the shared async Redis client (call from app lifespan)."""
    if not settings.redis_url:
        logger.info("REDIS_URL not set — memory caching disabled")
        return

    _holder.client = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
    )
    logger.info("Redis client initialised")


async def close_redis() -> None:
    """Shut down the Redis connection pool."""
    if _holder.client is not None:
        await _holder.client.aclose()
    _holder.client = None


def get_redis() -> aioredis.Redis | None:
    """Return the shared client, or None if Redis is not configured."""
    return _holder.client
