"""
Service TMDB — résolution titre → métadonnées (films et séries).
Utilise le Bearer token JWT (v4) via le header Authorization.
"""
import logging
from typing import Literal

import httpx

from app.config import settings
from app.models import ContentResult
from app.services import cache

logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p/w342"


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.tmdb_api_key}"}


def _year_from_date(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        return int(date_str[:4])
    except ValueError:
        return None


async def search_content(query: str, content_type: Literal["movie", "tv"] = "movie") -> list[ContentResult]:
    cache_key = f"tmdb:search:{content_type}:{query.lower()}"
    cached = await cache.get(cache_key)
    if cached:
        return [ContentResult(**f) for f in cached]

    endpoint = "movie" if content_type == "movie" else "tv"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{TMDB_BASE}/search/{endpoint}",
            headers=_headers(),
            params={"query": query, "language": "fr-BE", "region": "BE", "include_adult": False},
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("results", [])[:8]:
        if content_type == "movie":
            title = item.get("title") or item.get("original_title", "")
            original_title = item.get("original_title", "")
            year = _year_from_date(item.get("release_date"))
        else:
            title = item.get("name") or item.get("original_name", "")
            original_title = item.get("original_name", "")
            year = _year_from_date(item.get("first_air_date"))

        results.append(ContentResult(
            tmdb_id=item["id"],
            content_type=content_type,
            title=title,
            original_title=original_title,
            year=year,
            synopsis=item.get("overview") or None,
            poster_url=f"{TMDB_IMG_BASE}{item['poster_path']}" if item.get("poster_path") else None,
        ))

    await cache.set(cache_key, [r.model_dump() for r in results], ttl=settings.cache_ttl)
    return results


async def get_content_details(tmdb_id: int, content_type: Literal["movie", "tv"] = "movie") -> ContentResult | None:
    cache_key = f"tmdb:detail:{content_type}:{tmdb_id}"
    cached = await cache.get(cache_key)
    if cached:
        return ContentResult(**cached)

    endpoint = "movie" if content_type == "movie" else "tv"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{TMDB_BASE}/{endpoint}/{tmdb_id}",
            headers=_headers(),
            params={"language": "fr-BE"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        item = resp.json()

    if content_type == "movie":
        title = item.get("title") or item.get("original_title", "")
        original_title = item.get("original_title", "")
        year = _year_from_date(item.get("release_date"))
        result = ContentResult(
            tmdb_id=item["id"],
            content_type="movie",
            title=title,
            original_title=original_title,
            year=year,
            synopsis=item.get("overview") or None,
            poster_url=f"{TMDB_IMG_BASE}{item['poster_path']}" if item.get("poster_path") else None,
            duration_min=item.get("runtime") or None,
        )
    else:
        title = item.get("name") or item.get("original_name", "")
        original_title = item.get("original_name", "")
        year = _year_from_date(item.get("first_air_date"))
        result = ContentResult(
            tmdb_id=item["id"],
            content_type="tv",
            title=title,
            original_title=original_title,
            year=year,
            synopsis=item.get("overview") or None,
            poster_url=f"{TMDB_IMG_BASE}{item['poster_path']}" if item.get("poster_path") else None,
            number_of_seasons=item.get("number_of_seasons"),
            number_of_episodes=item.get("number_of_episodes"),
        )

    await cache.set(cache_key, result.model_dump(), ttl=settings.cache_ttl)
    return result


# Alias rétrocompatibilité
async def search_films(query: str) -> list[ContentResult]:
    return await search_content(query, "movie")

async def get_film_details(tmdb_id: int) -> ContentResult | None:
    return await get_content_details(tmdb_id, "movie")
