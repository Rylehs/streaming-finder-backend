from enum import Enum
from typing import Literal
from pydantic import BaseModel


class AccessType(str, Enum):
    subscription = "subscription"
    free = "free"
    rent = "rent"
    buy = "buy"


class StreamingOffer(BaseModel):
    platform: str
    platform_logo: str | None
    access_type: AccessType
    price_eur: float | None = None
    currency: str | None = None
    qualities: list[str] = []
    french_audio: bool | None = None
    french_subtitles: bool | None = None


class ContentResult(BaseModel):
    tmdb_id: int
    content_type: Literal["movie", "tv"]
    title: str
    original_title: str
    year: int | None
    synopsis: str | None
    poster_url: str | None
    # Film
    duration_min: int | None = None
    # Série
    number_of_seasons: int | None = None
    number_of_episodes: int | None = None


# Alias pour rétrocompatibilité avec le code existant
FilmResult = ContentResult


class AvailabilityEvent(BaseModel):
    event: str  # "film_meta" | "offer" | "done" | "error"
    data: dict
