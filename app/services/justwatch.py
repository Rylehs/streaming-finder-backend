"""
Service JustWatch — disponibilité streaming en Belgique via leur API GraphQL.

JustWatch n'a pas d'API publique officielle, mais leur API GraphQL interne
est utilisée par de nombreux projets open-source (ex: dawids justwatch-api).
Endpoint : https://apis.justwatch.com/graphql

Monétisation JustWatch → notre AccessType :
  FLATRATE / FLATRATE_AND_BUY  → subscription
  FREE / ADS                   → free
  RENT                         → rent
  BUY                          → buy
"""
import logging
import re
from typing import AsyncGenerator

import httpx

from app.config import settings
from app.models import AccessType, StreamingOffer
from app.services import cache

logger = logging.getLogger(__name__)

JUSTWATCH_GRAPHQL = "https://apis.justwatch.com/graphql"
JUSTWATCH_IMG_BASE = "https://images.justwatch.com"


def _logo_url(icon_template: str | None) -> str | None:
    """Construit l'URL complète du logo depuis le template JustWatch.
    Ex: /icon/169478387/{profile}/play.{format} → https://images.justwatch.com/icon/.../s100/play.webp
    """
    if not icon_template:
        return None
    return (
        JUSTWATCH_IMG_BASE
        + icon_template.replace("{profile}", "s100").replace("{format}", "webp")
    )

# JustWatch utilise des codes langue ISO 639-1 pour l'affichage,
# et des codes pays ISO 3166-1 alpha-2 pour le marché.
COUNTRY = "BE"
LANGUAGE = "fr"

MONETIZATION_MAP: dict[str, AccessType] = {
    "FLATRATE": AccessType.subscription,
    "FLATRATE_AND_BUY": AccessType.subscription,
    "FREE": AccessType.free,
    "ADS": AccessType.free,
    "RENT": AccessType.rent,
    "BUY": AccessType.buy,
}

QUERY_SEARCH = """
query SearchContent($query: String!, $country: Country!, $language: Language!, $objectType: ObjectType!) {
  searchTitles(
    first: 5
    source: "justwatch"
    filter: { searchQuery: $query, objectTypes: [$objectType] }
    country: $country
    language: $language
  ) {
    edges {
      node {
        ... on Movie {
          id
          content(country: $country, language: $language) {
            title
            originalTitle
            externalIds { tmdbId }
          }
          offers(country: $country, platform: WEB) {
            monetizationType
            presentationType
            retailPrice(language: $language)
            currency
            audioLanguages
            subtitleLanguages
            package { packageId clearName icon }
          }
        }
        ... on Show {
          id
          content(country: $country, language: $language) {
            title
            originalTitle
            externalIds { tmdbId }
          }
          offers(country: $country, platform: WEB) {
            monetizationType
            presentationType
            retailPrice(language: $language)
            currency
            audioLanguages
            subtitleLanguages
            package { packageId clearName icon }
          }
        }
      }
    }
  }
}
"""


QUALITY_ORDER = {"SD": 0, "HD": 1, "_4K": 2, "4K": 2}
QUALITY_LABELS = {"_4K": "4K"}


def _extract_price(raw: str | None) -> float | None:
    """Extrait un float depuis une string localisée ex: '3,99€' → 3.99"""
    if not raw:
        return None
    m = re.search(r"\d+[.,]\d+|\d+", str(raw))
    return float(m.group().replace(",", ".")) if m else None


def _parse_offers(raw_offers: list[dict], tmdb_id: int) -> list[StreamingOffer]:
    """
    Convertit les offres JustWatch brutes en StreamingOffer groupées par plateforme.
    Une offre = une plateforme + un type d'accès, avec toutes les qualités disponibles.
    """
    # grouped[(platform, access_type)] = {logo, min_price, currency, qualities, fr_audio, fr_subs}
    grouped: dict[tuple, dict] = {}

    for offer in raw_offers:
        monetization = offer.get("monetizationType", "")
        access_type = MONETIZATION_MAP.get(monetization)
        if access_type is None:
            continue

        pkg = offer.get("package") or {}
        platform = pkg.get("clearName", "Inconnu")
        quality = offer.get("presentationType") or ""

        # Filtre langue française
        audio_langs = [l.lower() for l in (offer.get("audioLanguages") or [])]
        sub_langs = [l.lower() for l in (offer.get("subtitleLanguages") or [])]
        has_fr_audio = "fr" in audio_langs
        has_fr_subs = "fr" in sub_langs

        if audio_langs and not has_fr_audio and not has_fr_subs:
            continue

        price_float = _extract_price(offer.get("retailPrice"))
        key = (platform, access_type)

        if key not in grouped:
            grouped[key] = {
                "platform_logo": _logo_url(pkg.get("icon")),
                "currency": offer.get("currency"),
                "min_price": price_float,
                "qualities": set(),
                "french_audio": has_fr_audio if audio_langs else None,
                "french_subtitles": has_fr_subs if sub_langs else None,
            }
        else:
            g = grouped[key]
            # Prix minimum parmi les qualités
            if price_float is not None:
                g["min_price"] = (
                    min(g["min_price"], price_float)
                    if g["min_price"] is not None
                    else price_float
                )
            # Si une variante a audio FR, on marque la plateforme comme audio FR
            if has_fr_audio:
                g["french_audio"] = True
            if has_fr_subs and not g.get("french_audio"):
                g["french_subtitles"] = True

        if quality:
            grouped[key]["qualities"].add(QUALITY_LABELS.get(quality, quality))

    # Convertir en StreamingOffer, qualités triées SD < HD < 4K
    offers = []
    for (platform, access_type), g in grouped.items():
        sorted_qualities = sorted(
            g["qualities"],
            key=lambda q: QUALITY_ORDER.get(q, 1),
        )
        offers.append(
            StreamingOffer(
                platform=platform,
                platform_logo=g["platform_logo"],
                access_type=access_type,
                price_eur=g["min_price"],
                currency=g["currency"],
                qualities=sorted_qualities,
                french_audio=g["french_audio"],
                french_subtitles=g["french_subtitles"],
            )
        )

    # Tri : subscription > free > rent > buy, puis prix croissant
    order = {
        AccessType.subscription: 0,
        AccessType.free: 1,
        AccessType.rent: 2,
        AccessType.buy: 3,
    }
    offers.sort(key=lambda o: (order[o.access_type], o.price_eur or 0))
    return offers


async def get_offers_by_title(
    title: str,
    tmdb_id: int | None = None,
    content_type: str = "movie",
) -> list[StreamingOffer]:
    object_type = "SHOW" if content_type == "tv" else "MOVIE"
    cache_key = f"jw:offers:be:{content_type}:{tmdb_id or title.lower()}"
    cached = await cache.get(cache_key)
    if cached:
        return [StreamingOffer(**o) for o in cached]

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            JUSTWATCH_GRAPHQL,
            json={
                "query": QUERY_SEARCH,
                "variables": {
                    "query": title,
                    "country": COUNTRY,
                    "language": LANGUAGE,
                    "objectType": object_type,
                },
            },
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        data = resp.json()

    edges = data.get("data", {}).get("searchTitles", {}).get("edges", [])
    if not edges:
        return []

    best_node = None
    for edge in edges:
        node = edge.get("node", {})
        ext_ids = (node.get("content", {}) or {}).get("externalIds", {}) or {}
        if tmdb_id and str(tmdb_id) == str(ext_ids.get("tmdbId")):
            best_node = node
            break
    if best_node is None:
        best_node = edges[0].get("node", {})

    raw_offers = best_node.get("offers") or [] if best_node else []
    offers = _parse_offers(raw_offers, tmdb_id or 0)

    await cache.set(cache_key, [o.model_dump() for o in offers], ttl=settings.cache_ttl)
    return offers


async def stream_offers(
    title: str,
    tmdb_id: int | None = None,
    content_type: str = "movie",
) -> AsyncGenerator[StreamingOffer, None]:
    offers = await get_offers_by_title(title, tmdb_id, content_type)
    for offer in offers:
        yield offer
