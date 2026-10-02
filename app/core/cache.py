"""Cache-aside helper for read-heavy catalogue endpoints.

Keys are namespaced by a global catalogue version: `catalog:v{version}:{key}`. Any catalogue
write bumps the version (`INCR catalog:version`), which orphans every old entry at once — no
key scanning or per-key deletes needed; orphans simply expire after `cache_ttl_seconds`.

The cache is strictly optional and FAILS OPEN: if Redis is down or slow, reads go straight to
the database and a warning is logged. Values must be JSON-serialisable (store response data,
never ORM objects).
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.redis import get_redis

logger = structlog.get_logger(__name__)

VERSION_KEY = "catalog:version"
_REDIS_ERRORS = (RedisError, OSError)


async def cached(key: str, loader: Callable[[], Awaitable[Any]]) -> Any:
    """Return the cached value for `key`, or call `loader`, cache its result and return it."""
    if not settings.cache_enabled:
        return await loader()

    redis = get_redis()
    try:
        version = await redis.get(VERSION_KEY) or "0"
        full_key = f"catalog:v{version}:{key}"
        hit = await redis.get(full_key)
    except _REDIS_ERRORS as exc:
        logger.warning("cache_read_failed", key=key, error=str(exc))
        return await loader()

    if hit is not None:
        return json.loads(hit)

    value = await loader()
    try:
        await redis.set(full_key, json.dumps(value), ex=settings.cache_ttl_seconds)
    except _REDIS_ERRORS as exc:
        logger.warning("cache_write_failed", key=key, error=str(exc))
    return value


async def invalidate_catalog() -> None:
    """Invalidate every catalogue cache entry by bumping the namespace version.

    If Redis is unavailable the bump is skipped; stale entries can then live for at most
    `cache_ttl_seconds`.
    """
    if not settings.cache_enabled:
        return
    try:
        await get_redis().incr(VERSION_KEY)
    except _REDIS_ERRORS as exc:
        logger.warning("cache_invalidate_failed", error=str(exc))
