"""
Support physique bol.com — Bing HTML + Wayback Machine CDX + JSON-LD.

Flux :
  1a. Bing HTML search (site:bol.com, async, sans clé) → URLs produits
  1b. Wayback Machine CDX API (public, non bloqué) → URLs produits archivées
  2.  Fetch de chaque page produit bol.com → extraction JSON-LD (prix, stock)
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
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-BE,fr;q=0.9,nl-BE;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Regex URL produit bol.com
_BOL_PRODUCT_RE = re.compile(
    r'https?://(?:www\.)?bol\.com(/(?:be|nl)/[a-z]{2}/p/[^\s"\'?#<>]+)'
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


def _normalize_bol_url(raw: str) -> str | None:
    """Extrait et normalise une URL produit bol.com."""
    m = _BOL_PRODUCT_RE.search(raw)
    if not m:
        return None
    path = m.group(1).rstrip("/") + "/"
    return f"https://www.bol.com{path}"


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


async def _bing_search(
    title: str, client: httpx.AsyncClient, max_results: int = 8
) -> list[str]:
    """
    Bing HTML search (sans clé API).
    - Supporte site:bol.com correctement (contrairement à DDG)
    - Moins agressif sur les IP cloud que DDG
    """
    query = f'"{title}" (blu-ray OR dvd OR "4K UHD") site:bol.com'
    url = f"https://www.bing.com/search?q={quote_plus(query)}&setlang=fr&cc=BE&count=15"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-BE,fr;q=0.9,en;q=0.8",
    }
    try:
        resp = await client.get(url, headers=headers, follow_redirects=True, timeout=7)
        if resp.status_code != 200:
            logger.debug("Bing status %d", resp.status_code)
            return []

        seen: set[str] = set()
        urls: list[str] = []

        # Bing encode les URLs cibles dans data-href="" ou href="" des résultats
        for raw in re.findall(
            r'(?:href|data-href)="(https?://(?:www\.)?bol\.com/[^"?#]+)"',
            resp.text
        ):
            norm = _normalize_bol_url(raw)
            if norm and norm not in seen:
                seen.add(norm)
                urls.append(norm)

        logger.info("Bing found %d bol.com URLs for '%s'", len(urls), title)
        return urls[:max_results]
    except Exception as e:
        logger.warning("Bing search error: %s", e)
        return []


async def _wayback_search(
    title: str, client: httpx.AsyncClient, max_results: int = 6
) -> list[str]:
    """
    Wayback Machine CDX API — toujours accessible depuis les IPs cloud.
    Trouve les URLs de pages produit bol.com archivées via le slug du titre.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:30]

    # On essaie les deux chemins bol.com BE (fr + nl)
    cdx_base = "https://web.archive.org/cdx/search/cdx"
    paths = [f"www.bol.com/be/nl/p/{slug}", f"www.bol.com/be/fr/p/{slug}"]

    async def _cdx_query(prefix: str) -> list[str]:
        try:
            resp = await client.get(
                cdx_base,
                params={
                    "url": prefix + "*",
                    "output": "json",
                    "fl": "original",
                    "collapse": "urlkey",
                    "filter": "statuscode:200",
                    "limit": "6",
                    "from": "20230101",
                },
                timeout=7,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            result = []
            for row in data[1:]:  # ligne 0 = en-tête
                original = row[0] if isinstance(row, list) else ""
                norm = _normalize_bol_url(original) if original else None
                if norm:
                    result.append(norm)
            return result
        except Exception as e:
            logger.debug("CDX query error (%s): %s", prefix, e)
            return []

    results = await asyncio.gather(*[_cdx_query(p) for p in paths], return_exceptions=True)

    seen: set[str] = set()
    urls: list[str] = []
    for batch in results:
        if isinstance(batch, list):
            for u in batch:
                if u not in seen:
                    seen.add(u)
                    urls.append(u)

    logger.info("Wayback CDX found %d URLs for slug '%s'", len(urls), slug)
    return urls[:max_results]


async def search_physical(
    title: str,
    original_title: str,
    year: int | None,
    tmdb_id: int,
    content_type: str = "movie",
) -> list[PhysicalOffer]:
    cache_key = f"bol:physical:v7:{tmdb_id}:{content_type}"
    cached = await cache.get(cache_key)
    if cached:
        return [PhysicalOffer(**o) for o in cached]

    search_title = original_title or title

    async with httpx.AsyncClient() as client:
        # Sources en parallèle
        bing_urls, wayback_urls = await asyncio.gather(
            _bing_search(search_title, client),
            _wayback_search(search_title, client),
            return_exceptions=True,
        )

        if isinstance(bing_urls, Exception):
            logger.warning("Bing task error: %s", bing_urls)
            bing_urls = []
        if isinstance(wayback_urls, Exception):
            logger.warning("Wayback task error: %s", wayback_urls)
            wayback_urls = []

        # Fusion + déduplication (Bing en priorité)
        seen: set[str] = set()
        all_urls: list[str] = []
        for u in list(bing_urls) + list(wayback_urls):
            if u not in seen:
                seen.add(u)
                all_urls.append(u)

        logger.info(
            "URLs for '%s': %d total (Bing: %d, Wayback: %d)",
            search_title, len(all_urls), len(bing_urls), len(wayback_urls),
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
