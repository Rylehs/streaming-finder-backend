"""
Service bol.com — support physique (DVD / Blu-ray / 4K UHD) en Belgique.

Credentials gratuits : https://developers.bol.com
Variables : BOL_CLIENT_ID + BOL_CLIENT_SECRET
"""
import logging
import time

import httpx

from app.config import settings
from app.models import PhysicalFormat, PhysicalOffer
from app.services import cache

logger = logging.getLogger(__name__)

BOL_TOKEN_URL = "https://login.bol.com/token"
BOL_CATALOG_URL = "https://api.bol.com/catalog/v4/products"

# Ordre important : 4K détecté avant Blu-ray (sinon "4K Blu-ray" → Blu-ray)
FORMAT_PATTERNS: list[tuple[list[str], PhysicalFormat]] = [
    (["4k", "4k uhd", "uhd", "ultra hd", "ultra-hd"], PhysicalFormat.uhd_4k),
    (["blu-ray", "blu ray", "bluray", "blu‑ray"],      PhysicalFormat.bluray),
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

_token_store: dict = {}


async def _get_token() -> str:
    now = time.time()
    if _token_store.get("access_token") and _token_store.get("expires_at", 0) > now + 60:
        return _token_store["access_token"]

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            BOL_TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(settings.bol_client_id, settings.bol_client_secret),
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

    _token_store["access_token"] = data["access_token"]
    _token_store["expires_at"] = now + data.get("expires_in", 3600)
    return _token_store["access_token"]


def _detect_format(title: str) -> PhysicalFormat | None:
    lower = title.lower()
    for keywords, fmt in FORMAT_PATTERNS:
        if any(kw in lower for kw in keywords):
            return fmt
    return None  # pas un produit physique


def _detect_edition(title: str) -> str | None:
    lower = title.lower()
    for keyword, label in EDITION_PATTERNS:
        if keyword in lower:
            return label
    return None


async def search_physical(
    title: str,
    original_title: str,
    year: int | None,
    tmdb_id: int,
    content_type: str = "movie",
) -> list[PhysicalOffer]:
    if not settings.bol_client_id or not settings.bol_client_secret:
        return []

    cache_key = f"bol:physical:{tmdb_id}:{content_type}"
    cached = await cache.get(cache_key)
    if cached:
        return [PhysicalOffer(**o) for o in cached]

    try:
        token = await _get_token()
    except Exception as exc:
        logger.error("Bol.com token error: %s", exc)
        return []

    query = original_title or title

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                BOL_CATALOG_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                params={"q": query, "limit": "25", "sort": "relevance"},
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.error("Bol.com search error: %s", exc)
        return []

    FORMAT_ORDER = {PhysicalFormat.uhd_4k: 0, PhysicalFormat.bluray: 1, PhysicalFormat.dvd: 2}
    offers: list[PhysicalOffer] = []

    for product in data.get("products", []):
        p_title = product.get("title", "")
        if not p_title:
            continue

        fmt = _detect_format(p_title)
        if fmt is None:
            continue  # pas un support physique, on ignore

        edition = _detect_edition(p_title)
        price = None
        in_stock = False

        for offer in product.get("offers", []):
            avail = offer.get("availabilityCode", "")
            if avail == "AVAILABLE":
                in_stock = True
                price = offer.get("price")
                break
            elif avail in ("PREORDER", "BACKORDER") and price is None:
                price = offer.get("price")

        product_id = product.get("id")
        url = f"https://www.bol.com/be/nl/p/{product_id}/" if product_id else None
        image_url = product.get("mainImageUrl")

        offers.append(PhysicalOffer(
            title=p_title,
            retailer="bol.com",
            format=fmt,
            edition=edition,
            price_eur=float(price) if price is not None else None,
            url=url,
            image_url=image_url,
            in_stock=in_stock,
        ))

    # Tri : 4K > Blu-ray > DVD, éditions spéciales en premier, puis prix croissant
    offers.sort(key=lambda o: (
        FORMAT_ORDER.get(o.format, 3),
        0 if o.edition else 1,
        o.price_eur or 999,
    ))

    await cache.set(cache_key, [o.model_dump() for o in offers], ttl=settings.cache_ttl)
    return offers
