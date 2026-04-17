from typing import Literal

from fastapi import APIRouter, Query

from app.models import AccessType
from app.services import justwatch, tmdb

router = APIRouter(prefix="/availability", tags=["availability"])

ACCESS_LABELS: dict[AccessType, str] = {
    AccessType.subscription: "Inclus dans l'abonnement",
    AccessType.free: "Gratuit",
    AccessType.rent: "Location",
    AccessType.buy: "Achat",
}


@router.get("/{tmdb_id}")
async def get_availability(
    tmdb_id: int,
    type: Literal["movie", "tv"] = Query("movie"),
):
    content = await tmdb.get_content_details(tmdb_id, content_type=type)
    if content is None:
        return {"content": None, "offers": [], "message": f"Contenu TMDB #{tmdb_id} introuvable"}

    offers = await justwatch.get_offers_by_title(
        title=content.title, tmdb_id=tmdb_id, content_type=type
    )

    offers_data = [
        {**o.model_dump(), "access_label": ACCESS_LABELS.get(o.access_type, o.access_type)}
        for o in offers
    ]

    message = (
        "Aucune offre trouvée en Belgique avec audio ou sous-titres français."
        if not offers
        else "Recherche terminée."
    )

    return {"content": content.model_dump(), "offers": offers_data, "message": message}
