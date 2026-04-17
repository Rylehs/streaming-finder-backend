from pathlib import Path
from pydantic_settings import BaseSettings

_ENV_FILE = Path(__file__).parent.parent / ".env"


class Settings(BaseSettings):
    tmdb_api_key: str = ""
    redis_url: str = "redis://localhost:6379"
    cache_ttl: int = 43200
    country: str = "BE"
    language: str = "fr"
    allowed_origins: str = "*"  # ex: "https://mon-app.vercel.app" en production
    # Google Custom Search (optionnel) — 100 req/jour gratuits
    # Setup : https://programmablesearchengine.google.com/ + Google Cloud Console
    google_cse_key: str = ""
    google_cse_id:  str = ""

    class Config:
        # En local : charge le .env ; en production : variables d'environnement directes
        env_file = str(_ENV_FILE) if _ENV_FILE.exists() else None


settings = Settings()
