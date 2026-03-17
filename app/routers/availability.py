import asyncio
import json
import logging
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.models import AccessType
from app.services import justwatch, tmdb

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/availability", tags=["availability"])

ACCESS_LABELS: dict[AccessType, str] = {
    AccessType.subscription: "Inclus dans l'abonnement",
    AccessType.free: "Gratuit",
    AccessType.rent: "Location",
    AccessType.buy: "Achat",
}


def _sse_event(event: str, data: dict) -> str:
    payload = json.dumps({"event": event, "data": data}, ensure_ascii=False)
    return f"data: {payload}\n\n"


@router.get("/{tmdb_id}")
async def stream_availability(
    tmdb_id: int,
    type: Literal["movie", "tv"] = Query("movie"),
):
    async def event_generator():
        try:
            content = await tmdb.get_content_details(tmdb_id, content_type=type)
            if content is None:
                yield _sse_event("error", {"message": f"Contenu TMDB #{tmdb_id} introuvable"})
                return
            yield _sse_event("film_meta", content.model_dump())
        except Exception as e:
            logger.exception("Erreur TMDB")
            yield _sse_event("error", {"message": f"Erreur TMDB : {e}"})
            return

        try:
            offer_count = 0
            async for offer in justwatch.stream_offers(
                title=content.title, tmdb_id=tmdb_id, content_type=type
            ):
                yield _sse_event("offer", {
                    **offer.model_dump(),
                    "access_label": ACCESS_LABELS.get(offer.access_type, offer.access_type),
                })
                offer_count += 1
                await asyncio.sleep(0)

            msg = "Aucune offre trouvée en Belgique avec audio ou sous-titres français." if offer_count == 0 else "Recherche terminée."
            yield _sse_event("done", {"message": msg, "total": offer_count})

        except Exception as e:
            logger.exception("Erreur JustWatch")
            yield _sse_event("error", {"message": f"Erreur disponibilité : {e}"})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
