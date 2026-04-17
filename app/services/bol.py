"""
Support physique bol.com — DuckDuckGo (sans clé API) + scraping page de recherche + JSON-LD.

Flux :
  1a. DuckDuckGo text search (query simple, sans site:) → URLs produits bol.com
  1b. Scraping direct page de recherche bol.com/be → URLs produits (/be/*/p/...)
  2.  Fetch de chaque page produit → extraction JSON-LD (prix, stock, titre)
  3.  Tri par qualité (4K > Blu-ray > DVD) puis prix
"""
import asyncio
import json as json_lib
import logging
import re
from typing import Any
from urllib.parse import quote_plus

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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
    "Accept-Language": "fr-BE,fr;q=0.9,nl-BE;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Regex pour extraire des URLs produits bol.com depuis du HTML brut
_BOL_PRODUCT_RE = re.compile(
    r'(?:https?://(?:www\.)?bol\.com)?(/be/[a-z]{2}/p/[^\s"\'?#<>]+)'
)


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


def _extract_bol_urls(html: str, max_results: int = 12) -> list[str]:
    """Extrait les URLs produits bol.com d'une page HTML."""
    seen: set[str] = set()
    urls: list[str] = []
    for m in _BOL_PRODUCT_RE.finditer(html):
        path = m.group(1).rstrip("/") + "/"
        url = f"https://www.bol.com{path}"
        if url not in seen:
            seen.add(url)
            urls.append(url)
        if len(urls) >= max_results:
            break
    return urls


async def _fetch_product(url: str, client: httpx.AsyncClient) -> PhysicalOffer | None:
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


def _ddg_search(query: str, max_results: int = 10) -> list[str]:
    """
    Recherche DuckDuckGo synchrone → liste d'URLs bol.com.
    NOTE: site:bol.com/be ne fonctionne pas dans DDG (chemin non supporté).
          On cherche sans opérateur site: et on filtre ensuite.
    """
    try:
        from duckduckgo_search import DDGS
        # On demande 4× plus de résultats car on va filtrer sur bol.com
        results = DDGS().text(query, max_results=max_results * 4)
        urls = [
            r["href"] for r in (results or [])
            if "bol.com" in r.get("href", "") and "/p/" in r.get("href", "")
        ]
        logger.debug("DDG returned %d bol.com product URLs", len(urls))
        return urls[:max_results]
    except Exception as exc:
        logger.error("DuckDuckGo search error: %s", exc)
        return []


async def _bol_search_page(title: str, client: httpx.AsyncClient) -> list[str]:
    """
    Scrape la page de résultats de recherche bol.com/be directement.
    Plus fiable que DDG car indépendant des limites de l'API de recherche.
    """
    query = f"{title} blu-ray dvd"
    url = f"https://www.bol.com/be/fr/s/?q={quote_plus(query)}"
    try:
        resp = await client.get(url, headers=_BOL_HEADERS, follow_redirects=True, timeout=10)
        if resp.status_code != 200:
            logger.debug("bol.com search page returned %d", resp.status_code)
            return []
        urls = _extract_bol_urls(resp.text)
        logger.debug("bol.com search page found %d product URLs", len(urls))
        return urls
    except Exception as e:
        logger.debug("bol.com search page error: %s", e)
        return []


async def search_physical(
    title: str,
    original_title: str,
    year: int | None,
    tmdb_id: int,
    content_type: str = "movie",
) -> list[PhysicalOffer]:
    cache_key = f"bol:physical:v5:{tmdb_id}:{content_type}"
    cached = await cache.get(cache_key)
    if cached:
        return [PhysicalOffer(**o) for o in cached]

    search_title = original_title or title
    # Query DDG sans opérateur site: — on mentionne simplement "bol.com" pour biaiser les résultats
    ddg_query = f'"{search_title}" blu-ray bol.com'

    async with httpx.AsyncClient() as client:
        loop = asyncio.get_event_loop()

        # DDG (synchrone → executor) + scraping page bol.com en parallèle
        ddg_future = loop.run_in_executor(None, _ddg_search, ddg_query, 8)
        ddg_urls, bol_urls = await asyncio.gather(
            ddg_future,
            _bol_search_page(search_title, client),
            return_exceptions=True,
        )

        if isinstance(ddg_urls, Exception):
            logger.warning("DDG task error: %s", ddg_urls)
            ddg_urls = []
        if isinstance(bol_urls, Exception):
            logger.warning("bol search page error: %s", bol_urls)
            bol_urls = []

        # Fusion + déduplication (DDG en priorité)
        seen: set[str] = set()
        all_urls: list[str] = []
        for u in list(ddg_urls) + list(bol_urls):
            if u not in seen:
                seen.add(u)
                all_urls.append(u)

        logger.info(
            "bol.com URLs for '%s': %d total (DDG: %d, search page: %d)",
            search_title, len(all_urls), len(ddg_urls), len(bol_urls),
        )

        if not all_urls:
            return []

        results = await asyncio.gather(
            *[_fetch_product(url, client) for url in all_urls[:10]],
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
