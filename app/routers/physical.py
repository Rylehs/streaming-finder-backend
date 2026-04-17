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

    offers = await bol.search_physical(
        title=content.title,
        original_title=content.original_title,
        year=content.year,
        tmdb_id=tmdb_id,
        content_type=type,
    )

    return {"offers": [o.model_dump() for o in offers], "total": len(offers)}
