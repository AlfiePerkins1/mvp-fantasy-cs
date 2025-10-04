import requests
import os
from dotenv import load_dotenv


load_dotenv(".env")

BASE_URL = "https://open.faceit.com/data/v4"
API_KEY = os.getenv("FACEIT_API_KEY")

HEADERS = {"Authorization": f"Bearer {API_KEY}"}

async def get_faceit_player_by_steam(steam64: str, game="cs2"):
    """Return FACEIT player info by Steam64. Tries cs2 then csgo if not found."""
    url = f"{BASE_URL}/players"
    r = requests.get(url, params={"game": game, "game_player_id": steam64}, headers=HEADERS, timeout=10)
    if r.status_code == 404 and game == "cs2":
        # fallback to csgo
        r = requests.get(url, params={"game": "csgo", "game_player_id": steam64}, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


async def get_faceit_stats(player_id: str, game="cs2"):
    """Return FACEIT stats for a given player_id."""
    url = f"{BASE_URL}/players/{player_id}/stats/{game}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()