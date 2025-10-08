# backend/services/pricing.py
from datetime import datetime, timezone

from sqlalchemy import select

from typing import List, Dict

from backend.models import User, Player
from backend.services.repo import leetify_l100_avg, upsert_player_ratings_and_l100
from backend.services.leetify_api import fetch_profile, extract_ranks

# Pricing config

P_MIN = 1_000            # floor price
P_MAX = 11_000           # ceiling price
GAMMA = 2             # makes top players expensive

# feature weights into a single skill score
W_LEETIFY = 0.50
W_FACEIT  = 0.25
W_PREMIER = 0.20
W_RENOWN  = 0.05

# scaling assumptions (clip to bounds)
FACEIT_MIN, FACEIT_MAX   = 400, 3800 # Max is gonna be 3.8k elo (for the moment)
PREMIER_MIN, PREMIER_MAX = 1000, 33000 # prem capped at 33k
RENOWN_MIN, RENOWN_MAX   = 3000, 25000 # Renown capped at 25k
LEETIFY_MIN, LEETIFY_MAX = -5.0, 5.0  # clamp to -5, 5 (should cover all bounds)


# Helper functions

def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x

def _norm(val: float | None, lo: float, hi: float) -> float:
    if val is None:
        return 0.0
    val = _clip(float(val), lo, hi)
    rng = (hi - lo) if hi != lo else 1.0
    return (val - lo) / rng

def _norm_leetify(v: float | None) -> float:
    # map [-5, +5] -> [0,1]
    return _norm(v, LEETIFY_MIN, LEETIFY_MAX)

def _price_from_percentile(p: float) -> int:
    p = _clip(p, 0.0, 1.0)
    # top-heavy curve
    return int(round(P_MIN + (P_MAX - P_MIN) * (p ** GAMMA)))


async def refresh_one_player(session, discord_id: str) -> dict:
    print(f' Discord ID being used: {discord_id}')
    u = await session.scalar(select(User).where(User.discord_id == str(discord_id)))
    if not u:
        print(f' Failed for Discord ID: {discord_id}')
        return {"discord_id": discord_id, "ok": False, "reason": "no_user"}

    print(f' Steam ID being used: {u.steam_id}')
    profile = await fetch_profile(u.steam_id)
    ranks = extract_ranks(profile) if profile else {
        "renown_elo": None, "premier_elo": None, "faceit_elo": None
    }
    print(ranks)

    l100 = await leetify_l100_avg(session, u.id)

    print(l100)

    ok = await upsert_player_ratings_and_l100(
        session, str(discord_id),
        renown_elo=ranks["renown_elo"],
        premier_elo=ranks["premier_elo"],
        faceit_elo=ranks["faceit_elo"],
        l100=l100
    )
    return {"discord_id": discord_id, "ok": ok, **ranks, "leetify_l100_avg": l100}


async def compute_and_persist_prices(session) -> List[Dict]:
    result = await session.execute(
        select(
            Player.id,
            Player.handle,
            Player.faceit_elo,
            Player.premier_elo,
            Player.renown_elo,
            Player.leetify_l100_avg
        )
    )
    rows = result.all()
    if not rows:
        return []

    # build skill scores (no early returns here)
    scored: List[tuple[int, float, str]] = []  # (player_id, score, handle)
    for pid, handle, faceit, premier, renown, l100 in rows:
        leetify_norm = _norm_leetify(l100)
        faceit_norm  = _norm(faceit,  FACEIT_MIN,  FACEIT_MAX)
        premier_norm = _norm(premier, PREMIER_MIN, PREMIER_MAX)
        renown_norm  = _norm(renown,  RENOWN_MIN,  RENOWN_MAX)

        score = (
            W_LEETIFY * leetify_norm +
            W_FACEIT  * faceit_norm  +
            W_PREMIER * premier_norm +
            W_RENOWN  * renown_norm
        )
        scored.append((pid, score, handle))

    # convert to percentiles (across the whole pool)
    scored.sort(key=lambda t: t[1])
    n = len(scored)
    percentiles: Dict[int, float] = {}
    for i, (pid, _, _) in enumerate(scored):
        p = 0.5 if n <= 1 else (i / (n - 1))
        percentiles[pid] = p

    # persist updates
    updated: List[Dict] = []
    now = datetime.now(timezone.utc)

    has_price         = hasattr(Player, "price")
    has_skill_score   = hasattr(Player, "skill_score")
    has_percentile    = hasattr(Player, "percentile")
    has_price_updated = hasattr(Player, "price_updated_at")

    ids = [pid for pid, _, _ in scored]
    players = (
        await session.execute(
            select(Player).where(Player.id.in_(ids))   # <-- in_()
        )
    ).scalars().all()
    pmap = {p.id: p for p in players}

    for pid, score, handle in scored:
        p = percentiles[pid]
        price = _price_from_percentile(p)

        obj = pmap.get(pid)
        if not obj:
            continue

        if has_price:
            obj.price = price
        if has_skill_score:
            obj.skill_score = score
        if has_percentile:
            obj.percentile = p
        if has_price_updated:
            obj.price_updated_at = now

        updated.append({
            "player_id": pid,
            "handle": handle,
            "score": round(score, 6),
            "percentile": round(p, 4),
            "price": price,
        })

    # return once, after processing everyone
    return updated

