from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.services import bol, tmdb

router = APIRouter(prefix="/physical", tags=["physical"])


@router.get("/{tmdb_id}")
async def get_physical(
    tmdb_id: int,
    type: Literal["movie", "tv"] = Query("movie"),
):
    content = await tmdb.get_content_details(tmdb_id, content_type=type)
    if content is None:
        raise HTTPException(status_code=404, detail=f"Contenu TMDB #{tmdb_id} introuvable")

    # Recherche parallèle : vérification existence + produits bol.com
    import asyncio
    has_physical, offers = await asyncio.gather(
        bol.has_physical_release(tmdb_id, content_type=type),
        bol.search_physical(
            title=content.title,
            original_title=content.original_title,
            year=content.year,
            tmdb_id=tmdb_id,
            content_type=type,
        ),
    )

    return {
        "has_physical": has_physical,
        "title": content.title,
        "original_title": content.original_title,
        "year": content.year,
        "offers": [o.model_dump() for o in offers],
    }


@router.get("/debug/{tmdb_id}")
async def debug_physical(
    tmdb_id: int,
    type: Literal["movie", "tv"] = Query("movie"),
):
    """Endpoint de diagnostic — à supprimer après débogage."""
    content = await tmdb.get_content_details(tmdb_id, content_type=type)
    if content is None:
        raise HTTPException(status_code=404, detail="Contenu introuvable")

    search_title = content.original_title or content.title
    has_google = bool(settings.google_cse_key and settings.google_cse_id)
    has_serp = bool(settings.serpapi_key)

    async with httpx.AsyncClient() as client:
        # 1. Récupérer les URLs
        urls: list[str] = []
        if has_google:
            urls = await bol._google_cse_search(search_title, client, max_results=4)
        elif has_serp:
            urls = await bol._serpapi_search(search_title, client, max_results=4)

        # 2. Tester le premier URL : statut HTTP + extrait JSON-LD
        fetch_samples = []
        for url in urls[:3]:
            try:
                resp = await client.get(url, headers=bol._BOL_HEADERS,
                                        follow_redirects=True, timeout=8)
                product = bol._parse_jsonld(resp.text) if resp.status_code == 200 else None
                fetch_samples.append({
                    "url": url,
                    "status": resp.status_code,
                    "product_found": product is not None,
                    "product_name": product.get("name") if product else None,
                    "product_format": bol._detect_format(product.get("name", "")) if product else None,
                    "html_snippet": resp.text[resp.text.find("ld+json"):resp.text.find("ld+json") + 500] if "ld+json" in resp.text else "no ld+json",
                })
            except Exception as exc:
                fetch_samples.append({"url": url, "error": str(exc)})

    return {
        "title": content.title,
        "original_title": content.original_title,
        "has_google": has_google,
        "has_serp": has_serp,
        "serpapi_key_set": bool(settings.serpapi_key),
        "urls_found": urls,
        "fetch_samples": fetch_samples,
    }
