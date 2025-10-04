# services/ingest_user.py
from backend.services.repo import get_or_create_user
from backend.services.leetify_api import fetch_recent_matches, parse_finished_at_to_london
from backend.services.repo_ingest import upsert_match, upsert_player_game

async def ingest_user_recent_matches(session, *, discord_id: int, limit: int = 50) -> dict:
    user = await get_or_create_user(session, discord_id)
    if not user.steam_id:
        return {"fetched": 0, "inserted": 0, "no_row": 0}

    matches = await fetch_recent_matches(user.steam_id, limit=limit)
    fetched = len(matches)
    inserted = 0
    no_row = 0

    for m in matches:
        match_id = await upsert_match(session, m)
        row = next((s for s in (m.get("stats") or []) if str(s.get("steam64_id")) == str(user.steam_id)), None)
        if not row:
            no_row += 1
            continue
        await upsert_player_game(
            session,
            user_id=user.id,
            steam_id=str(user.steam_id),
            match_id=match_id,
            row=row,
            m=m,
        )
        inserted += 1

    await session.flush()
    return {"fetched": fetched, "inserted": inserted, "no_row": no_row}
