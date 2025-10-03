from statistics import mean
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from datetime import datetime, timedelta, timezone
from ..models import WeeklyPoints

MATCH_MULT = {
    "faceit": 1.20,
    "renown": 1.10,
    "premier": 1.00,
    "mm": 0.8
}

def _v(x, d=0.0): return float(x) if x is not None else float(d)
def _i(x, d=0): return int(x) if x is not None else int(d)

def compute_weekly_from_playerstats(ps, *, alpha: float = 10.0, k: float = 0.60, cap: float = 1.15):

    # games
    g_counts = int(_v(ps.premier_games,0)) + int(_v(ps.faceit_games,0)) + int(_v(ps.renown_games,0)) + int(_v(ps.mm_games, 0))
    games_total = int(ps.sample_size or g_counts or 0)
    if games_total <= 0:
        return{
            "base_avg": 0.0,
            "avg_mult": 1.0,
            "weekly_base": 0.0,
            "weekly_score": 0.0
        }

    base_avg = (
        10 * _v(ps.avg_leetify_rating) +
        0.1 * _v(ps.adr) +
        2.0 * _v(ps.trade_kills) +
        3.0 * _v(ps.entries) +
        1.0 * _v(ps.flashes) +
        0.05 * _v(ps.util_dmg)
    )

    n_prem = int(_v(ps.premier_games,0))
    n_faceit = int(_v(ps.faceit_games,0))
    n_ren = int(_v(ps.renown_games,0))
    n_mm = int(_v(ps.mm_games,0))
    denom = max(1, n_prem + n_faceit + n_ren + n_mm)

    avg_mult = (
                       MATCH_MULT["premier"] * n_prem +
                       MATCH_MULT["faceit"] * n_faceit +
                       MATCH_MULT["renown"] * n_ren +
                       MATCH_MULT["mm"] * n_mm
               ) / denom

    weekly_base = base_avg * avg_mult

    # 3) win-rate multiplier (shrink to 50% for small samples)
    wins = int(_v(ps.wins, 0))
    wr_eff = (wins + alpha * 0.5) / (games_total + alpha)
    wr_mult = min(1.0 + max(0.0, wr_eff - 0.5) * k, cap)

    weekly_score = weekly_base * wr_mult

    return {
        "base_avg": round(base_avg, 3),
        "avg_mult": round(avg_mult, 3),
        "weekly_base": round(weekly_base, 3),
        "weekly_score": round(weekly_score, 3),
    }



def make_breakdown(ps, *, alpha: float = 10.0, k: float = 0.60, cap: float = 1.15, w=None):
    w = w or {"rating": 10.0, "adr": 0.1, "trades": 2.0, "entries": 3.0, "flashes": 1.0, "util": 0.05}

    games_total = _i(ps.sample_size) or (
            _i(ps.premier_games) + _i(ps.faceit_games) + _i(ps.renown_games) + _i(ps.mm_games)
    )
    if games_total <= 0:
        return dict(
            sample_size=0, wins=_i(ps.wins),
            faceit_games=_i(ps.faceit_games), premier_games=_i(ps.premier_games),
            renown_games=_i(ps.renown_games), mm_games=_i(ps.mm_games),
            pts_rating=0, pts_adr=0, pts_trades=0, pts_entries=0, pts_flashes=0, pts_util=0,
            base_avg=0, avg_mult=1.0, wr_eff=0.5, wr_mult=1.0, weekly_score=0.0
        )

    pts_rating = w["rating"] * _v(ps.avg_leetify_rating)
    pts_adr = w["adr"] * _v(ps.adr)
    pts_trades = w["trades"] * (_v(ps.trade_kills))
    pts_entries = w["entries"] * (_v(ps.entries))
    pts_flashes = w["flashes"] * (_v(ps.flashes))
    pts_util = w["util"] * (_v(ps.util_dmg))

    base_avg = pts_rating + pts_adr + pts_trades + pts_entries + pts_flashes + pts_util

    n_p = _i(ps.premier_games);
    n_f = _i(ps.faceit_games);
    n_r = _i(ps.renown_games);
    n_m = _i(ps.mm_games)
    denom = max(1, n_p + n_f + n_r + n_m)
    avg_mult = (MATCH_MULT["premier"] * n_p + MATCH_MULT["faceit"] * n_f + MATCH_MULT["renown"] * n_r + MATCH_MULT[
        "mm"] * n_m) / denom

    weekly_base = base_avg * avg_mult

    wins = _i(ps.wins)
    wr_eff = (wins + alpha * 0.5) / (games_total + alpha)
    wr_mult = min(1.0 + max(0.0, wr_eff - 0.5) * k, cap)

    weekly_score = weekly_base * wr_mult

    return dict(
        sample_size=games_total, wins=wins,
        faceit_games=n_f, premier_games=n_p, renown_games=n_r, mm_games=n_m,
        pts_rating=pts_rating, pts_adr=pts_adr, pts_trades=pts_trades,
        pts_entries=pts_entries, pts_flashes=pts_flashes, pts_util=pts_util,
        base_avg=base_avg, avg_mult=avg_mult, wr_eff=wr_eff, wr_mult=wr_mult,
        weekly_score=weekly_score
    )

async def upsert_weekly_points(session, *, week_start, guild_id, user_id, ruleset_id, ps):
    bd = make_breakdown()

    stmt = sqlite_insert(WeeklyPoints).values(
        week_start=week_start, guild_id=guild_id, user_id=user_id, ruleset_id=ruleset_id,
        **bd
    ).on_conflict_do_update(
        index_elements=["week_start", "guild_id", "user_id"],
        set_={
            **{k: getattr(sqlite_insert(WeeklyPoints).excluded, k) for k in bd.keys()},
            "computed_at": datetime.now(timezone.utc),
            "ruleset_id": ruleset_id,
        }
    )
    await session.execute(stmt)
