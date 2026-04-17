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
    bol_client_id: str = ""
    bol_client_secret: str = ""

    class Config:
        # En local : charge le .env ; en production : variables d'environnement directes
        env_file = str(_ENV_FILE) if _ENV_FILE.exists() else None


settings = Settings()
