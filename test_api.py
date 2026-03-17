"""
Tests rapides à lancer avec : python test_api.py
Nécessite le serveur démarré : uvicorn app.main:app --reload
"""
import httpx
import json


BASE = "http://localhost:8000"


def test_search():
    print("\n=== RECHERCHE 'Astérix' ===")
    r = httpx.get(f"{BASE}/search", params={"q": "Astérix"})
    films = r.json()
    for f in films[:3]:
        print(f"  [{f['tmdb_id']}] {f['title']} ({f['year']}) — {f['poster_url']}")
    return films[0]["tmdb_id"] if films else None


def test_availability(tmdb_id: int):
    print(f"\n=== DISPONIBILITÉ tmdb_id={tmdb_id} (Belgique, FR) ===")
    with httpx.stream("GET", f"{BASE}/availability/{tmdb_id}") as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                etype = event["event"]
                data = event["data"]
                if etype == "film_meta":
                    print(f"  Film : {data['title']} ({data['year']}) — {data['duration_min']} min")
                elif etype == "offer":
                    price = f"{data['price_eur']}€" if data["price_eur"] else "inclus/gratuit"
                    fr_audio = "🔊FR" if data.get("french_audio") else ("📝FR" if data.get("french_subtitles") else "")
                    print(f"  {data['access_label']:30} {data['platform']:20} {price:10} {data['quality'] or ''} {fr_audio}")
                elif etype == "done":
                    print(f"  → {data['message']} ({data['total']} offres)")
                elif etype == "error":
                    print(f"  ERREUR : {data['message']}")


if __name__ == "__main__":
    tmdb_id = test_search()
    if tmdb_id:
        test_availability(tmdb_id)
