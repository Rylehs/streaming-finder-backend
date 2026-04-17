"""
Support physique bol.com — DDG HTML interface + scraping bol.com + JSON-LD.

Flux :
  1a. DDG HTML interface (POST async, sans bibliothèque) → URLs produits bol.com
  1b. Scraping direct page de recherche bol.com/be → URLs produits
  2.  Fetch de chaque page produit → extraction JSON-LD (prix, stock, titre)
  3.  Tri par qualité (4K > Blu-ray > DVD) puis prix
"""
import asyncio
import json as json_lib
import logging
import re
from typing import Any
from urllib.parse import quote_plus, unquote

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

_DDG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-BE,fr;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": "https://duckduckgo.com/",
    "Origin": "https://duckduckgo.com",
}

# Regex : URLs produits bol.com (chemin /be/xx/p/...)
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
    """Extrait les URLs produits bol.com depuis du HTML brut."""
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


async def _ddg_html_search(
    query: str, client: httpx.AsyncClient, max_results: int = 8
) -> list[str]:
    """
    Recherche via l'interface HTML de DuckDuckGo (POST, sans bibliothèque).
    Les URLs sont encodées dans le paramètre uddg= de chaque lien de résultat.
    """
    try:
        resp = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "kl": "be-fr"},
            headers=_DDG_HEADERS,
            follow_redirects=True,
            timeout=8,
        )
        if resp.status_code != 200:
            logger.debug("DDG HTML status %d", resp.status_code)
            return []

        urls: list[str] = []
        seen: set[str] = set()
        # Chaque résultat DDG a href="/l/?uddg=<URL-encodée>"
        for m in re.finditer(r'uddg=(https?[^&"\'<\s]+)', resp.text):
            decoded = unquote(m.group(1))
            if "bol.com" in decoded and "/p/" in decoded:
                clean = decoded.split("?")[0].rstrip("/") + "/"
                if clean not in seen:
                    seen.add(clean)
                    urls.append(clean)

        logger.info("DDG HTML found %d bol.com URLs for '%s'", len(urls), query)
        return urls[:max_results]
    except Exception as e:
        logger.warning("DDG HTML search error: %s", e)
        return []


async def _bol_search_page(
    title: str, client: httpx.AsyncClient
) -> list[str]:
    """
    Scrape la page de résultats bol.com/be.
    La page est rendue côté serveur : les liens produit sont dans le HTML.
    """
    query = f"{title} blu-ray dvd"
    url = f"https://www.bol.com/be/fr/s/?q={quote_plus(query)}"
    try:
        resp = await client.get(
            url, headers=_BOL_HEADERS, follow_redirects=True, timeout=8
        )
        if resp.status_code != 200:
            logger.debug("bol.com search page status %d", resp.status_code)
            return []

        urls = _extract_bol_urls(resp.text)
        logger.info("bol.com search page found %d URLs for '%s'", len(urls), title)
        return urls
    except Exception as e:
        logger.warning("bol.com search page error: %s", e)
        return []


async def search_physical(
    title: str,
    original_title: str,
    year: int | None,
    tmdb_id: int,
    content_type: str = "movie",
) -> list[PhysicalOffer]:
    cache_key = f"bol:physical:v6:{tmdb_id}:{content_type}"
    cached = await cache.get(cache_key)
    if cached:
        return [PhysicalOffer(**o) for o in cached]

    search_title = original_title or title
    ddg_query = f'"{search_title}" blu-ray dvd bol.com'

    async with httpx.AsyncClient() as client:
        # Deux sources en parallèle
        ddg_urls, bol_urls = await asyncio.gather(
            _ddg_html_search(ddg_query, client, 8),
            _bol_search_page(search_title, client),
            return_exceptions=True,
        )

        if isinstance(ddg_urls, Exception):
            logger.warning("DDG task error: %s", ddg_urls)
            ddg_urls = []
        if isinstance(bol_urls, Exception):
            logger.warning("bol search page task error: %s", bol_urls)
            bol_urls = []

        # Fusion + déduplication (DDG en priorité)
        seen: set[str] = set()
        all_urls: list[str] = []
        for u in list(ddg_urls) + list(bol_urls):
            if u not in seen:
                seen.add(u)
                all_urls.append(u)

        logger.info(
            "Total URLs for '%s': %d (DDG: %d, bol page: %d)",
            search_title, len(all_urls), len(ddg_urls), len(bol_urls),
        )

        if not all_urls:
            return []

        results = await asyncio.gather(
            *[_fetch_product(url, client) for url in all_urls[:8]],
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
