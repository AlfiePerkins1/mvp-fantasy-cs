from datetime import datetime, timezone

import requests
import os
from dotenv import load_dotenv
import httpx

load_dotenv(".env")

BASE_URL = "https://open.faceit.com/data/v4"
FACEIT_ELO_MATCH_URL = ("https://api.faceit.com/stats/v1/stats/time/users/{player}/games/cs2")

API_KEY = os.getenv("FACEIT_API_KEY")

FACEIT_HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
}

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

async def fetch_faceit_guid_by_steam(steam64: str, game="cs2") -> str | None:
    """Return FACEIT guid by Steam64."""
    url = "https://open.faceit.com/data/v4/players"
    params = {"game": "cs2", "game_player_id": str(steam64)}

    # Wrote this at a different time to the above hence the httpx instead
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params, headers=FACEIT_HEADERS)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        # The Faceit GUID is in `player_id`
        return data.get("player_id") or None


def _to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)

async def fetch_faceit_match_elo_for_player(
        faceit_player_id: str,
        since: datetime = datetime(2024, 1, 1, tzinfo=timezone.utc),
        until: datetime | None = None,
        page: int = 0,
        size: int = 2000,
) -> list[dict]:
    """
    Returns a list of dicts for the player, each item contains id.matchID & elo
    """

    if until is None:
        until = datetime.now(timezone.utc)

        params = {
            "size": size,
            "page": page,
            "from": _to_ms(since),
            "to": _to_ms(until),
        }

        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                FACEIT_ELO_MATCH_URL.format(player=faceit_player_id), params=params
            )
            r.raise_for_status()
            data = r.json()

        if isinstance(data, list):
            out = []
            for d in data:
                if isinstance(d, dict):
                    if "matchId" not in d and "_id" in d and isinstance(d["_id"], dict):
                        mid = d["_id"].get("matchId")
                        if mid is not None:
                            d = dict(d)  # shallow copy
                            d["matchId"] = mid
                    out.append(d)
            return out


        return []