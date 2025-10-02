from statistics import mean

MATCH_MULT = {
    "faceit": 1.20,
    "renown": 1.10,
    "premier": 1.00,
    "mm": 0.8
}

def _v(x, default=0.0):
    return float(x) if x is not None else float(default)

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