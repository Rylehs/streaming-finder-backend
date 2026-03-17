import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Redis est optionnel — fallback mémoire si non disponible
_memory_cache: dict[str, Any] = {}

try:
    import redis.asyncio as aioredis
    from app.config import settings
    _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    _use_redis = True
except Exception:
    _redis_client = None
    _use_redis = False
    logger.warning("Redis non disponible, cache en mémoire utilisé.")


async def get(key: str) -> Any | None:
    if _use_redis:
        try:
            value = await _redis_client.get(key)
            return json.loads(value) if value else None
        except Exception as e:
            logger.warning(f"Cache Redis get error: {e}")
    return _memory_cache.get(key)


async def set(key: str, value: Any, ttl: int) -> None:
    if _use_redis:
        try:
            await _redis_client.setex(key, ttl, json.dumps(value))
            return
        except Exception as e:
            logger.warning(f"Cache Redis set error: {e}")
    _memory_cache[key] = value
