"""
Support physique — confirmation de sortie via TMDB release_dates.
Aucun credential requis.
"""
import logging

import httpx

from app.config import settings
from app.services import cache

logger = logging.getLogger(__name__)
TMDB_BASE = "https://api.themoviedb.org/3"


async def has_physical_release(tmdb_id: int, content_type: str = "movie") -> bool:
    """
    Vérifie si une sortie physique (type 5 = DVD/Blu-ray) existe via TMDB.
    Pour les séries, retourne toujours True (TMDB ne suit pas les sorties physiques TV).
    """
    if content_type == "tv":
        return True

    cache_key = f"physical:exists:movie:{tmdb_id}"
    cached = await cache.get(cache_key)
    if cached is not None:
        return bool(cached)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{TMDB_BASE}/movie/{tmdb_id}/release_dates",
                headers={"Authorization": f"Bearer {settings.tmdb_api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()

        # type 5 = Physical / Home Video
        for entry in data.get("results", []):
            for release in entry.get("release_dates", []):
                if release.get("type") == 5:
                    await cache.set(cache_key, True, ttl=settings.cache_ttl)
                    return True

        await cache.set(cache_key, False, ttl=settings.cache_ttl)
        return False

    except Exception as exc:
        logger.warning("TMDB release_dates error (assume physical exists): %s", exc)
        return True  # En cas d'erreur on suppose qu'une sortie physique existe
