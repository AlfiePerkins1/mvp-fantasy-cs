# repo_ingest.py
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from datetime import timezone

from backend.services.leetify_api import parse_finished_at_to_london

from ..models import Match, PlayerGame

def _v(x, d=0.0): return float(x) if x is not None else float(d)
def _i(x, d=0):   return int(x) if x is not None else int(d)

def _as_utc(dt):  # simple helper
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

async def upsert_match(session, m: dict) -> int:
    """Create/get Match row and return match_id."""
    stmt = sqlite_insert(Match).values(
        data_source=m["data_source"],
        source_match_id=str(m["data_source_match_id"]),
        finished_at=_as_utc(parse_finished_at_to_london(m["finished_at"]).astimezone(timezone.utc)),
        map_name=m.get("map_name"),
        replay_url=m.get("replay_url"),
        has_banned_player=bool(m.get("has_banned_player")),
        team1_number=(m.get("team_scores") or [{}])[0].get("team_number"),
        team1_score =(m.get("team_scores") or [{}])[0].get("score"),
        team2_number=(m.get("team_scores") or [{}, {}])[1].get("team_number"),
        team2_score =(m.get("team_scores") or [{}, {}])[1].get("score"),
    ).on_conflict_do_nothing(index_elements=["data_source","source_match_id"])
    await session.execute(stmt)

    # fetch id
    match_id = await session.scalar(
        select(Match.id).where(
            Match.data_source == m["data_source"],
            Match.source_match_id == str(m["data_source_match_id"])
        )
    )
    return int(match_id)

async def upsert_player_game(session, *, user_id: int | None, steam_id: str, match_id: int, row: dict, m: dict) -> None:
    """Insert one PlayerGame line for this steam_id and match. No-op on duplicate."""
    # compute 'won'
    player_team = row.get("initial_team_number")
    ts = m.get("team_scores") or []
    my = next((t.get("score") for t in ts if t.get("team_number") == player_team), None)
    opp= next((t.get("score") for t in ts if t.get("team_number") != player_team), None)
    won = (my is not None and opp is not None and my > opp)

    finished_utc = _as_utc(parse_finished_at_to_london(m["finished_at"]).astimezone(timezone.utc))

    stmt = sqlite_insert(PlayerGame).values(
        user_id=user_id,
        steam_id=str(steam_id),
        match_id=match_id,
        finished_at=finished_utc,
        data_source=m["data_source"],

        initial_team_number=row.get("initial_team_number"),
        rounds_count=row.get("rounds_count"),
        rounds_won=row.get("rounds_won"),
        rounds_lost=row.get("rounds_lost"),
        won=won,

        leetify_rating=row.get("leetify_rating"),
        ct_leetify_rating=row.get("ct_leetify_rating"),
        t_leetify_rating=row.get("t_leetify_rating"),

        total_kills=row.get("total_kills"),
        total_deaths=row.get("total_deaths"),
        total_assists=row.get("total_assists"),
        kd_ratio=row.get("kd_ratio"),

        dpr=row.get("dpr"),
        he_foes_damage_avg=row.get("he_foes_damage_avg"),
        flashbang_leading_to_kill=row.get("flashbang_leading_to_kill"),
        trade_kills_succeed=row.get("trade_kills_succeed"),
    ).on_conflict_do_nothing(index_elements=["steam_id","match_id"])

    await session.execute(stmt)


