# services/ingest_user.py
from backend.services.repo import get_or_create_user, get_user
from backend.services.leetify_api import fetch_recent_matches, parse_finished_at_to_london
from backend.services.repo_ingest import upsert_match, upsert_player_game
from backend.models import User
from sqlalchemy import select

async def ingest_user_recent_matches(session, *, steam_id: int | str | None = None, discord_id: int | str | None = None, guild_id: int | str | None = None, limit: int = 50) -> dict:
    resolved_user = None
    if steam_id is None:
        if discord_id is None or guild_id is None:
            raise ValueError("Provide steam_id OR (discord_id and guild_id).")
        resolved_user = await get_user(session, discord_id=discord_id, guild_id=guild_id)
        if not resolved_user or not resolved_user.steam_id:
            return {"fetched": 0, "inserted": 0, "no_row": 0, "reason": "no user or no steam linked"}
        steam_id = int(resolved_user.steam_id)
    else:
        steam_id = int(steam_id)
        # If caller provided a discord/guild, try to find that specific row
        if discord_id is not None and guild_id is not None:
            resolved_user = await get_user(session, discord_id=discord_id, guild_id=guild_id)
        # Otherwise, try to attach ANY user row for this steam (optional)
        if resolved_user is None:
            any_uid = await session.scalar(
                select(User.id).where(User.steam_id == steam_id).order_by(User.id.asc())
            )
            if any_uid is not None:
                class _U: pass

                resolved_user = _U()
                resolved_user.id = any_uid

    # -------- fetch from API --------
    matches = await fetch_recent_matches(steam_id, limit=limit)
    fetched = len(matches)
    inserted = 0
    no_row = 0

    # -------- upsert match + player_game for THIS steam --------
    for m in matches:
        match_id = await upsert_match(session, m)

        stats_list = m.get("stats") or []
        row = next((s for s in stats_list if str(s.get("steam64_id")) == str(steam_id)), None)
        if not row:
            no_row += 1
            continue

        await upsert_player_game(
            session,
            user_id=(resolved_user.id if resolved_user is not None else None),  # OK if your schema allows NULL
            steam_id=str(steam_id),
            match_id=match_id,
            row=row,
            m=m,
        )
        inserted += 1

    await session.flush()
    return {"fetched": fetched, "inserted": inserted, "no_row": no_row}
