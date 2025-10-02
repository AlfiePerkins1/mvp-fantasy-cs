import httpx
from typing import Any, Dict, List, Optional
from statistics import mean
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

def aggregate_player_stats(matches: List[Dict[str, Any]], steam_id: str, week_start_london: datetime | None = None) -> Dict[str, Any]:

    week_start_london = week_start_london or current_week_start_london()

    print("Leetify matches total:", len(matches))

    leetify_vals: List[float] = []
    ct_vals: List[float] = []
    t_vals: List[float] = []
    trade_kills_total = 0

    total = len(matches)
    in_week = 0
    rows_found = 0
    ratings_found = 0

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

        lr = _safe_float(row.get("leetify_rating"))
        ctr = _safe_float(row.get("ct_leetify_rating"))
        tr = _safe_float(row.get("t_leetify_rating"))

        # If they're not none then append them and multiply by 100 since the API returns them divided by 100 (idk why)
        if lr is not None: leetify_vals.append(lr * 100); ratings_found += 1
        if ctr is not None: ct_vals.append(ctr * 100)
        if tr is not None: t_vals.append(tr * 100)

        tk = _safe_int(row.get("trade_kills_succeed"))
        if tk: trade_kills_total += tk

    print(f"Leetify total={total} in week = {in_week} rows_found = {rows_found} ratings_found = {ratings_found} week_start = {week_start_london}")
    print("Leetify ratings used: ", [f"{x:.4f}" for x in t_vals])

    return  {
            "avg_leetify_rating": (sum(leetify_vals) / len(leetify_vals)) if leetify_vals else None,
            "ct_rating": (sum(ct_vals) / len(ct_vals)) if ct_vals else None,
            "t_rating": (sum(t_vals) / len(t_vals)) if t_vals else None,
            "sample_size": len(leetify_vals),
            "trade_kills": trade_kills_total,
        }
