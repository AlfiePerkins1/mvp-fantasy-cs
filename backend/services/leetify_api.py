import httpx
from typing import Any, Dict, List, Optional
from statistics import fmean
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


LEETIFY_BASE = "https://api-public.cs-prod.leetify.com/v3/profile/matches"

async def fetch_recent_matches(steam_id: str, limit: int = 100) -> List[Dict[str, Any]]:

    url = f"{LEETIFY_BASE}?steam64_id={steam_id}"

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()

        if isinstance(data, list):
            return data
        return []

def _safe_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (ValueError, TypeError):
        return None

def _safe_int(x: Any) -> Optional[int]:
    try:
        return int(x)
    except (ValueError, TypeError):
        return None


def current_week_start_london(now: datetime | None = None) -> datetime:
    now = now or datetime.now(tz=ZoneInfo("Europe/London"))

    days_since_monday = now.weekday()
    week_start = (now - timedelta(days=days_since_monday)).replace(hour=0,minute=0,second=0, microsecond=0)
    return week_start

def parse_finished_at_to_london(ts: str) -> datetime:
    try:
        dt_utc = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt_utc.astimezone(ZoneInfo("Europe/London"))
    except Exception:
        return None

def aggregate_player_stats(matches: List[Dict[str, Any]],
                           steam_id: str,
                           week_start_london: Optional[datetime] = None) -> Dict[str, Any]:
    week_start_london = week_start_london or current_week_start_london()

    print("Leetify matches total:", len(matches))

    # Per-match arrays (we'll average them)
    leetify_vals: List[float] = []
    ct_vals: List[float] = []
    t_vals: List[float] = []
    adr_vals: List[float] = []
    util_dmg_vals: List[float] = []  # he_foes_damage_avg per match

    # Totals / counters
    trade_kills_total = 0
    entries_total = 0                 # Leetify doesn't expose yet
    flashes_total = 0                 # sum of flashbang_leading_to_kill

    faceit_games = 0
    premier_games = 0
    renown_games = 0
    mm_games = 0
    other_games = 0

    wins_total = 0

    total = len(matches)
    in_week = 0
    rows_found = 0
    ratings_found = 0

    SOURCE_MAP = {
        "faceit": "faceit",
        "renown": "renown",
        "matchmaking_competitive": "mm",
        "matchmaking": "mm",
        "matchmaking_wingman": "wingman",
        "matchmaking_premier": "premier",
        "premier": "premier",
    }

    for m in matches:
        finished_at_london = parse_finished_at_to_london(m.get("finished_at", ""))
        if not finished_at_london or finished_at_london < week_start_london:
            continue
        in_week += 1

        stats = m.get("stats", []) or []
        row = next((s for s in stats if str(s.get("steam64_id")) == str(steam_id)), None)
        if not row:
            continue
        rows_found += 1

        # -------- Platform counter (use mapped values) --------
        cat = SOURCE_MAP.get(m.get("data_source"))
        if   cat == "faceit":   faceit_games += 1
        elif cat == "premier":  premier_games += 1
        elif cat == "renown":   renown_games += 1
        elif cat == "mm":       mm_games += 1
        elif cat == "wingman":  other_games += 1
        else:                   other_games += 1

        # -------- Win detection (fix key name) --------
        player_team = row.get("initial_team_number")  # was 'intial_team_number'
        team_scores = m.get("team_scores", []) or []
        try:
            my_team_score = next(ts["score"] for ts in team_scores if ts.get("team_number") == player_team)
            opp_team_score = next(ts["score"] for ts in team_scores if ts.get("team_number") != player_team)
            if my_team_score > opp_team_score:
                wins_total += 1
        except StopIteration:
            pass  # ignore malformed scores

        # -------- Ratings (convert 0–1 → 0–100 like your code) --------
        lr  = _safe_float(row.get("leetify_rating"))
        ctr = _safe_float(row.get("ct_leetify_rating"))
        tr  = _safe_float(row.get("t_leetify_rating"))
        if lr  is not None: leetify_vals.append(lr * 100); ratings_found += 1
        if ctr is not None: ct_vals.append(ctr * 100)
        if tr  is not None: t_vals.append(tr * 100)

        # -------- ADR & util --------
        adr_val = _safe_float(row.get("dpr"))
        if adr_val is not None:
            adr_vals.append(adr_val)

        util_avg = _safe_float(row.get("he_foes_damage_avg"))
        if util_avg is not None:
            util_dmg_vals.append(util_avg)

        # -------- Flashes (leading to kill) --------
        flashes_val = _safe_int(row.get("flashbang_leading_to_kill")) or 0
        flashes_total += flashes_val

        # -------- Trades --------
        tk = _safe_int(row.get("trade_kills_succeed")) or 0
        trade_kills_total += tk

    print(f"Leetify total={total} in week = {in_week} rows_found = {rows_found} ratings_found = {ratings_found} week_start = {week_start_london}")
    print("Leetify ratings used: ", [f"{x:.4f}" for x in t_vals])

    # Safe average helper
    def _avg(xs: List[float]) -> Optional[float]:
        return (sum(xs) / len(xs)) if xs else None

    avg_leetify = _avg(leetify_vals)
    ct_rating   = _avg(ct_vals)
    t_rating    = _avg(t_vals)
    avg_adr     = _avg(adr_vals)
    avg_util    = _avg(util_dmg_vals)

    # NOTE on types vs DB columns:
    # - adr is Float in your model → keep float
    # - flashes/util_dmg are Integer in your model → cast to int
    #   (util_dmg here is an average-per-match; if you prefer, change the column to Float.)

    return {
        "avg_leetify_rating": avg_leetify,
        "ct_rating": ct_rating,
        "t_rating": t_rating,
        "sample_size": len(leetify_vals),
        "trade_kills": int(trade_kills_total),

        "adr": avg_adr,                          # float or None
        "entries": int(entries_total),           # stays 0 until API exposes it
        "flashes": int(flashes_total),           # total flashes (int)
        "util_dmg": (int(round(avg_util)) if avg_util is not None else None),  # cast to int for DB

        "faceit_games": int(faceit_games),
        "premier_games": int(premier_games),
        "renown_games": int(renown_games),
        "mm_games": int(mm_games),
        "other_games": int(other_games),

        "wins": int(wins_total),
    }
