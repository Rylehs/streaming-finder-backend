"""
Support physique bol.com — Google Custom Search + JSON-LD.

Setup gratuit (5 min, ~100 req/jour) :
  1. cse.google.com/cse/create → moteur restreint à www.bol.com/be → copier l'ID (cx)
  2. console.cloud.google.com → Custom Search JSON API → créer une clé API
  3. vercel env add GOOGLE_API_KEY + GOOGLE_CSE_ID
"""
import json as json_lib
import logging
import re
from typing import Any

import httpx

from app.config import settings
from app.models import PhysicalFormat, PhysicalOffer
from app.services import cache

logger = logging.getLogger(__name__)

GOOGLE_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"

FORMAT_PATTERNS: list[tuple[list[str], PhysicalFormat]] = [
    (["4k", "4k uhd", "uhd", "ultra hd", "ultra-hd"], PhysicalFormat.uhd_4k),
    (["blu-ray", "blu ray", "bluray"],                 PhysicalFormat.bluray),
    (["dvd"],                                           PhysicalFormat.dvd),
]

EDITION_PATTERNS: list[tuple[str, str]] = [
    ("steelbook",           "Steelbook"),
    ("digibook",            "Digibook"),
    ("extended",            "Extended Edition"),
    ("director",            "Director's Cut"),
    ("collector",           "Collector's Edition"),
    ("ultimate",            "Ultimate Edition"),
    ("limited edition",     "Édition Limitée"),
    ("édition limitée",     "Édition Limitée"),
    ("intégrale",           "Intégrale"),
    ("complete collection", "Intégrale"),
    ("complete series",     "Intégrale"),
    ("trilogie",            "Trilogie"),
    ("trilogy",             "Trilogie"),
    ("coffret",             "Coffret"),
    ("box set",             "Coffret"),
    ("pack ",               "Pack"),
    ("collection",          "Collection"),
    ("special edition",     "Édition Spéciale"),
    ("édition spéciale",    "Édition Spéciale"),
]

_BOL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
    "Accept-Language": "fr-BE,fr;q=0.9,nl-BE;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _detect_format(title: str) -> PhysicalFormat | None:
    lower = title.lower()
    for keywords, fmt in FORMAT_PATTERNS:
        if any(kw in lower for kw in keywords):
            return fmt
    return None


def _detect_edition(title: str) -> str | None:
    lower = title.lower()
    for keyword, label in EDITION_PATTERNS:
        if keyword in lower:
            return label
    return None


def _parse_jsonld(html: str) -> dict[str, Any] | None:
    """Extrait le premier bloc JSON-LD de type Product dans le HTML."""
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL
    ):
        try:
            data = json_lib.loads(match.group(1).strip())
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") == "Product":
                    return item
        except Exception:
            continue
    return None


async def _fetch_product_data(url: str, client: httpx.AsyncClient) -> PhysicalOffer | None:
    """Récupère et parse les données d'une page produit bol.com."""
    try:
        resp = await client.get(url, headers=_BOL_HEADERS, follow_redirects=True, timeout=8)
        if resp.status_code != 200:
            return None
    except Exception as e:
        logger.debug("bol.com fetch error %s: %s", url, e)
        return None

    product = _parse_jsonld(resp.text)
    if not product:
        return None

    p_title = product.get("name", "")
    if not p_title:
        return None

    fmt = _detect_format(p_title)
    if fmt is None:
        return None

    edition = _detect_edition(p_title)

    # Prix
    offer_raw = product.get("offers", {})
    if isinstance(offer_raw, list):
        offer_raw = offer_raw[0] if offer_raw else {}
    price_val = offer_raw.get("price") or offer_raw.get("lowPrice")
    try:
        price = float(price_val) if price_val is not None else None
    except (ValueError, TypeError):
        price = None

    # Stock
    avail = offer_raw.get("availability", "")
    in_stock = any(s in avail for s in ("InStock", "OnlineOnly", "LimitedAvailability"))

    # Image
    img = product.get("image")
    image_url = img.get("url") if isinstance(img, dict) else img
    if not isinstance(image_url, str):
        image_url = None

    return PhysicalOffer(
        title=p_title,
        retailer="bol.com",
        format=fmt,
        edition=edition,
        price_eur=price,
        url=url,
        image_url=image_url,
        in_stock=in_stock,
    )


async def search_physical(
    title: str,
    original_title: str,
    year: int | None,
    tmdb_id: int,
    content_type: str = "movie",
) -> list[PhysicalOffer]:
    if not settings.google_api_key or not settings.google_cse_id:
        return []

    cache_key = f"bol:physical:v3:{tmdb_id}:{content_type}"
    cached = await cache.get(cache_key)
    if cached:
        return [PhysicalOffer(**o) for o in cached]

    # 1. Google Custom Search → URLs produits bol.com
    query = f'"{original_title or title}" blu-ray OR dvd OR "4K"'
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                GOOGLE_SEARCH_URL,
                params={"key": settings.google_api_key, "cx": settings.google_cse_id,
                        "q": query, "num": 10},
            )
            resp.raise_for_status()
            results = resp.json().get("items", [])
    except Exception as exc:
        logger.error("Google Search error: %s", exc)
        return []

    urls = [
        item["link"] for item in results
        if "bol.com" in item.get("link", "") and "/p/" in item.get("link", "")
    ][:8]

    if not urls:
        return []

    # 2. Fetch pages produit en parallèle → JSON-LD
    async with httpx.AsyncClient() as client:
        tasks = [_fetch_product_data(url, client) for url in urls]
        import asyncio
        raw = await asyncio.gather(*tasks, return_exceptions=True)

    offers: list[PhysicalOffer] = [o for o in raw if isinstance(o, PhysicalOffer)]

    # Tri : 4K > Blu-ray > DVD, éditions spéciales en premier, puis prix
    FORMAT_ORDER = {PhysicalFormat.uhd_4k: 0, PhysicalFormat.bluray: 1, PhysicalFormat.dvd: 2}
    offers.sort(key=lambda o: (
        FORMAT_ORDER.get(o.format, 3),
        0 if o.edition else 1,
        o.price_eur or 999,
    ))

    await cache.set(cache_key, [o.model_dump() for o in offers], ttl=settings.cache_ttl)
    return offers


async def has_physical_release(tmdb_id: int, content_type: str = "movie") -> bool:
    """Vérifie via TMDB si une sortie physique existe (type 5 = Home Video)."""
    if content_type == "tv":
        return True

    cache_key = f"physical:exists:movie:{tmdb_id}"
    cached = await cache.get(cache_key)
    if cached is not None:
        return bool(cached)

    try:
        from app.config import settings as s
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates",
                headers={"Authorization": f"Bearer {s.tmdb_api_key}"},
            )
            resp.raise_for_status()
            for entry in resp.json().get("results", []):
                for release in entry.get("release_dates", []):
                    if release.get("type") == 5:
                        await cache.set(cache_key, True, ttl=s.cache_ttl)
                        return True
        await cache.set(cache_key, False, ttl=settings.cache_ttl)
        return False
    except Exception as exc:
        logger.warning("TMDB release_dates error: %s", exc)
        return True
