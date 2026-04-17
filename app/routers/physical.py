from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from app.services import bol, tmdb

router = APIRouter(prefix="/physical", tags=["physical"])


@router.get("/{tmdb_id}")
async def get_physical(
    tmdb_id: int,
    type: Literal["movie", "tv"] = Query("movie"),
):
    content = await tmdb.get_content_details(tmdb_id, content_type=type)
    if content is None:
        raise HTTPException(status_code=404, detail=f"Contenu TMDB #{tmdb_id} introuvable")

    has_physical = await bol.has_physical_release(tmdb_id, content_type=type)

    return {
        "has_physical": has_physical,
        "title": content.title,
        "original_title": content.original_title,
        "year": content.year,
    }
