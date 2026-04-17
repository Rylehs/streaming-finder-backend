"""
Support physique bol.com — Google Custom Search (optionnel) + JSON-LD.

Pourquoi Google CSE ?
  bol.com bloque les IPs cloud (résultats de recherche chargés en JS, pas
  d'API publique, sitemaps inexploitables en temps réel). Bing/DDG retournent
  des pages CAPTCHA depuis les datacenter AWS de Vercel.

  Google Custom Search est la seule approche gratuite (100 req/jour) qui
  fonctionne depuis des IPs cloud. Elle nécessite une configuration unique :
    1. https://programmablesearchengine.google.com/ → créer un moteur "bol.com"
    2. Google Cloud Console → activer Custom Search API → créer une clé
    3. Ajouter GOOGLE_CSE_KEY et GOOGLE_CSE_ID dans les variables Vercel

  Si ces variables sont absentes, la section support physique est masquée.

Flux (quand Google CSE est configuré) :
  1. Google CSE : "{titre}" (blu-ray OR dvd OR "4K UHD") → URLs produits bol.com
  2. Fetch de chaque page produit → extraction JSON-LD (prix, stock, titre)
  3. Tri par qualité (4K > Blu-ray > DVD) puis prix
"""
import asyncio
import json as json_lib
import logging
import re
from typing import Any

import httpx

from app.config import settings
from app.models import PhysicalFormat, PhysicalOffer
from app.services import cache

logger = logging.getLogger(__name__)

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
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
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


async def _fetch_product(url: str, client: httpx.AsyncClient) -> PhysicalOffer | None:
    try:
        resp = await client.get(url, headers=_BOL_HEADERS, follow_redirects=True, timeout=6)
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

    offer_raw = product.get("offers", {})
    if isinstance(offer_raw, list):
        offer_raw = offer_raw[0] if offer_raw else {}

    price_val = offer_raw.get("price") or offer_raw.get("lowPrice")
    try:
        price = float(price_val) if price_val is not None else None
    except (ValueError, TypeError):
        price = None

    avail = offer_raw.get("availability", "")
    in_stock = any(s in avail for s in ("InStock", "OnlineOnly", "LimitedAvailability"))

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


async def _google_cse_search(
    title: str, client: httpx.AsyncClient, max_results: int = 8
) -> list[str]:
    """Google Custom Search API — nécessite GOOGLE_CSE_KEY + GOOGLE_CSE_ID."""
    if not settings.google_cse_key or not settings.google_cse_id:
        return []
    query = f'"{title}" (blu-ray OR dvd OR "4K UHD")'
    try:
        resp = await client.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": settings.google_cse_key, "cx": settings.google_cse_id,
                    "q": query, "num": min(max_results, 10), "gl": "be", "hl": "fr"},
            timeout=8,
        )
        if resp.status_code != 200:
            logger.warning("Google CSE status %d: %s", resp.status_code, resp.text[:200])
            return []
        data = resp.json()
        urls = [it["link"] for it in data.get("items", [])
                if "bol.com" in it.get("link", "") and "/p/" in it.get("link", "")]
        logger.info("Google CSE found %d URLs for '%s'", len(urls), title)
        return urls
    except Exception as e:
        logger.warning("Google CSE error: %s", e)
        return []


async def _serpapi_search(
    title: str, client: httpx.AsyncClient, max_results: int = 8
) -> list[str]:
    """SerpAPI — 100 req/mois gratuits, nécessite SERPAPI_KEY."""
    if not settings.serpapi_key:
        return []
    query = f'"{title}" (blu-ray OR dvd OR "4K UHD") site:bol.com'
    try:
        resp = await client.get(
            "https://serpapi.com/search.json",
            params={"q": query, "location": "Belgium", "hl": "fr", "gl": "be",
                    "api_key": settings.serpapi_key, "num": max_results},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("SerpAPI status %d: %s", resp.status_code, resp.text[:200])
            return []
        data = resp.json()
        urls = [
            r.get("link", "")
            for r in data.get("organic_results", [])
            if "bol.com" in r.get("link", "") and "/p/" in r.get("link", "")
        ]
        logger.info("SerpAPI found %d URLs for '%s'", len(urls), title)
        return urls
    except Exception as e:
        logger.warning("SerpAPI error: %s", e)
        return []


async def search_physical(
    title: str,
    original_title: str,
    year: int | None,
    tmdb_id: int,
    content_type: str = "movie",
) -> list[PhysicalOffer]:
    cache_key = f"bol:physical:v8:{tmdb_id}:{content_type}"
    cached = await cache.get(cache_key)
    if cached:
        return [PhysicalOffer(**o) for o in cached]

    # Sans aucune clé configurée, la recherche automatique est impossible
    has_google = bool(settings.google_cse_key and settings.google_cse_id)
    has_serp   = bool(settings.serpapi_key)
    if not has_google and not has_serp:
        logger.info("Aucune clé de recherche configurée — support physique désactivé")
        return []

    search_title = original_title or title

    async with httpx.AsyncClient() as client:
        # Utiliser la première API disponible
        if has_google:
            urls = await _google_cse_search(search_title, client, max_results=8)
        else:
            urls = await _serpapi_search(search_title, client, max_results=8)

        if not urls and title != original_title:
            if has_google:
                urls = await _google_cse_search(title, client, max_results=8)
            else:
                urls = await _serpapi_search(title, client, max_results=8)

        logger.info("Total URLs for '%s': %d", search_title, len(urls))

        if not urls:
            return []

        results = await asyncio.gather(
            *[_fetch_product(url, client) for url in urls],
            return_exceptions=True,
        )

    offers: list[PhysicalOffer] = [o for o in results if isinstance(o, PhysicalOffer)]

    FORMAT_ORDER = {PhysicalFormat.uhd_4k: 0, PhysicalFormat.bluray: 1, PhysicalFormat.dvd: 2}
    offers.sort(key=lambda o: (
        FORMAT_ORDER.get(o.format, 3),
        0 if o.edition else 1,
        o.price_eur or 999,
    ))

    await cache.set(cache_key, [o.model_dump() for o in offers], ttl=settings.cache_ttl)
    return offers


async def has_physical_release(tmdb_id: int, content_type: str = "movie") -> bool:
    if content_type == "tv":
        return True

    cache_key = f"physical:exists:movie:{tmdb_id}"
    cached = await cache.get(cache_key)
    if cached is not None:
        return bool(cached)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"https://api.themoviedb.org/3/movie/{tmdb_id}/release_dates",
                headers={"Authorization": f"Bearer {settings.tmdb_api_key}"},
            )
            resp.raise_for_status()
            for entry in resp.json().get("results", []):
                for release in entry.get("release_dates", []):
                    if release.get("type") == 5:
                        await cache.set(cache_key, True, ttl=settings.cache_ttl)
                        return True
        await cache.set(cache_key, False, ttl=settings.cache_ttl)
        return False
    except Exception as exc:
        logger.warning("TMDB release_dates error: %s", exc)
        return True
