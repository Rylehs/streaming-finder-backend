from typing import Literal
from fastapi import APIRouter, HTTPException, Query

from app.models import ContentResult
from app.services import tmdb

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=list[ContentResult])
async def search_content(
    q: str = Query(..., min_length=2),
    type: Literal["movie", "tv"] = Query("movie"),
):
    if not q.strip():
        raise HTTPException(status_code=400, detail="Titre vide")
    return await tmdb.search_content(q.strip(), content_type=type)
