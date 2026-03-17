from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import availability, search

app = FastAPI(
    title="Streaming Finder BE",
    description="Trouve où regarder un film en Belgique, en français.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins.split(","),
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(search.router)
app.include_router(availability.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
