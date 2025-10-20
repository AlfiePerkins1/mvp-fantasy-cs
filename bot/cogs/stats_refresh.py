import discord
from discord import app_commands
from discord.ext import commands


from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, case, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects import sqlite

from backend.db import SessionLocal
from backend.services.repo import upsert_stats
from backend.models import User, PlayerGame, WeeklyPoints
from backend.services.ingest_user import ingest_user_recent_matches

# Config
MATCH_MULT = {"premier": 1.20, "faceit": 1.10, "renown": 1.00, "mm": 0.80}


# Helpers
def breakdown_from_agg(a: dict, *, alpha=10.0, k=0.60, cap=1.15) -> dict:
    """Convert aggregated stats -> per-stat points + weekly_score."""
    games_counts = _i(a["premier_games"]) + _i(a["faceit_games"]) + _i(a["renown_games"]) + _i(a["mm_games"])
    games_total  = max(_i(a["sample_size"]), games_counts)
    if games_total <= 0:
        return {
            "sample_size": 0, "wins": 0,
            "faceit_games": 0, "premier_games": 0, "renown_games": 0, "mm_games": 0,
            "pts_rating": 0.0, "pts_adr": 0.0, "pts_trades": 0.0, "pts_entries": 0.0, "pts_flashes": 0.0, "pts_util": 0.0,
            "base_avg": 0.0, "avg_mult": 1.0, "wr_eff": 0.5, "wr_mult": 1.0, "weekly_score": 0.0,
        }

    pts_rating  = 10.0 * _v(a["avg_leetify_rating"])
    pts_adr     = 0.1  * _v(a["adr"])
    pts_trades  = 2.0  * (_v(a["trade_kills"]) / games_total)
    pts_entries = 3.0  * (_v(a["entries"]) / games_total)
    pts_flashes = 1.0  * (_v(a["flashes"]) / games_total)
    pts_util    = 0.05 * _v(a["util_dmg"])  # util is already an average

    base_avg = pts_rating + pts_adr + pts_trades + pts_entries + pts_flashes + pts_util

    denom = max(1, games_counts)
    avg_mult = (
        MATCH_MULT["premier"]*_i(a["premier_games"]) +
        MATCH_MULT["faceit"] *_i(a["faceit_games"]) +
        MATCH_MULT["renown"] *_i(a["renown_games"]) +
        MATCH_MULT["mm"]     *_i(a["mm_games"])
    ) / denom

    wins   = _i(a["wins"])
    wr_eff = (wins + alpha*0.5) / (games_total + alpha)
    wr_mult = min(1.0 + max(0.0, wr_eff - 0.5) * k, cap)

    return {
        "sample_size": games_total,
        "wins": wins,
        "faceit_games": _i(a["faceit_games"]),
        "premier_games": _i(a["premier_games"]),
        "renown_games": _i(a["renown_games"]),
        "mm_games": _i(a["mm_games"]),
        "pts_rating": pts_rating, "pts_adr": pts_adr, "pts_trades": pts_trades,
        "pts_entries": pts_entries, "pts_flashes": pts_flashes, "pts_util": pts_util,
        "base_avg": base_avg, "avg_mult": avg_mult, "wr_eff": wr_eff, "wr_mult": wr_mult,
        "weekly_score": base_avg * avg_mult * wr_mult,
    }

def _v(x, d=0.0): return float(x) if x is not None else float(d)
def _i(x, d=0):   return int(x) if x is not None else int(d)

def week_bounds_naive_utc(tz_name="Europe/London"):
    now_local = datetime.now(ZoneInfo(tz_name))
    start_local = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_local = start_local + timedelta(days=7)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc   = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_utc - timedelta(hours=1)
    return start_utc, end_utc

async def aggregate_week_from_db(
    session,
    *,
    user_id: int | None = None,
    steam_id: int | None = None,
    week_start_utc,                 # UTC-naive datetime
    week_end_utc=None               # optional UTC-naive datetime
) -> dict:
    assert (user_id is not None) ^ (steam_id is not None), "Pass exactly one of user_id or steam_id"
    if week_end_utc is None:
        week_end_utc = week_start_utc + timedelta(days=7)

    # sanity: your DB column is naive bounds must be naive
    if getattr(week_start_utc, "tzinfo", None) is not None or getattr(week_end_utc, "tzinfo", None) is not None:
        raise ValueError("Pass UTC-naive week bounds")

    PG = PlayerGame
    id_filter = (PG.user_id == user_id) if user_id is not None else (PG.steam_id == steam_id)

    stmt = (
        select(
            func.count(PG.id).label("sample_size"),
            func.avg(PG.leetify_rating * 100.0).label("avg_leetify_rating"),
            func.sum(PG.trade_kills_succeed).label("trade_kills"),
            func.avg(PG.dpr).label("adr"),
            func.sum(PG.flashbang_leading_to_kill).label("flashes"),
            func.avg(PG.he_foes_damage_avg).label("util_dmg"),
            func.avg(PG.ct_leetify_rating * 100).label("ct_rating"),
            func.avg(PG.t_leetify_rating * 100).label("t_rating"),
            func.sum(case((PG.won.is_(True), 1), else_=0)).label("wins"),

            # Buckets — EXACT mapping
            func.sum(case((PG.data_source == "matchmaking_competitive", 1), else_=0)).label("premier_games"),
            func.sum(case((PG.data_source == "matchmaking", 1), else_=0)).label("mm_games"),
            func.sum(case((PG.data_source == "faceit", 1), else_=0)).label("faceit_games"),
            func.sum(case((PG.data_source == "renown", 1), else_=0)).label("renown_games"),
        )
        .where(
            id_filter,
            PG.finished_at >= week_start_utc,
            PG.finished_at <  week_end_utc,
        )
    )
    print("STMT (literal):")
    print(stmt.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))


    res = await session.execute(stmt)
    m = res.mappings().one()  # RowMapping (immutable)

    # Build a normal dict with safe coercions
    sample_size = int(m["sample_size"] or 0)

    row = {
        "sample_size": sample_size,
        "avg_leetify_rating": (m["avg_leetify_rating"] if sample_size > 0 else None),
        "trade_kills": int(m["trade_kills"] or 0),
        "adr": (m["adr"] if sample_size > 0 else None),
        "flashes": int(m["flashes"] or 0),
        "util_dmg": (m["util_dmg"] if sample_size > 0 else None),
        "ct_rating": (m["ct_rating"] if sample_size > 0 else None),
        "t_rating": (m["t_rating"] if sample_size > 0 else None),
        "wins": int(m["wins"] or 0),

        # Buckets: Premier/MM/Faceit/Renown as you specified
        "premier_games": int(m["premier_games"] or 0),
        "mm_games": int(m["mm_games"] or 0),
        "faceit_games": int(m["faceit_games"] or 0),
        "renown_games": int(m["renown_games"] or 0),
    }

    row["other_games"] = max(
        0,
        sample_size
        - row["premier_games"] - row["mm_games"]
        - row["faceit_games"] - row["renown_games"]
    )
    row.setdefault("entries", 0.0)
    return row

async def upsert_weekly_points_from_breakdown(session, *, week_start_utc: datetime, guild_id: int, user_id: int, ruleset_id: int, bd: dict):
    stmt = sqlite_insert(WeeklyPoints).values(
        week_start=week_start_utc,
        guild_id=guild_id,
        user_id=user_id,
        ruleset_id=ruleset_id,
        computed_at=datetime.now(timezone.utc),
        **bd,
    )
    update_cols = {k: getattr(stmt.excluded, k) for k in bd.keys()}
    update_cols["computed_at"] = datetime.now(timezone.utc)
    update_cols["ruleset_id"]  = ruleset_id
    stmt = stmt.on_conflict_do_update(
        index_elements=["week_start", "guild_id", "user_id"],
        set_=update_cols,
    )
    await session.execute(stmt)

class stats(commands.Cog):
    """Update stats and backfill games"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    stats = app_commands.Group(name="stats", description="Update stats")

    @stats.command(name="backfill_games", description="Ingest recent matches for all registered users")
    @app_commands.describe(all_guilds="If true, process users from EVERY guild (use sparingly)")
    @app_commands.checks.has_permissions(administrator=True)
    async def backfill_games(self, interaction: discord.Interaction, limit: int = 100, all_guilds: bool = False):
        """
            Admin Only
            Fetches and stores recent matches for all registered users

            For each user, runs ingest_user_recent_matches() which gets match data from leetify API
            Match data then inserted into 'player_games' table (and related tables).

            Writes to: 'match', 'PlayerGame', 'PlayerStats'

            Use to populate historical match data (last 100 games) so player ratings and stats can be calculated.

        """
        guild_id = interaction.guild_id
        await interaction.response.defer(ephemeral=True, thinking=True)

        scope_guild_id = interaction.guild_id
        if not scope_guild_id and not all_guilds:
            await interaction.followup.send("Use this in a server (or pass all_guilds=True).", ephemeral=True)
            return

        total = 0
        errors: list[tuple[int, str]] = []

        async with SessionLocal() as session:
            q = select(User.steam_id)
            q = q.where(User.steam_id.is_not(None))
            if not all_guilds:
                q = q.where(User.discord_guild_id == scope_guild_id)

            steams = [int(s) for (s,) in (await session.execute(q)).all()]
            if not steams:
                scope = "all guilds" if all_guilds else f"this server ({scope_guild_id})"
                await interaction.followup.send(f"No registered users with Steam IDs in {scope}.", ephemeral=True)
                return

            for steam in sorted(set(steams)):
                try:
                    await ingest_user_recent_matches(session, steam_id=steam, limit=limit)
                    total += 1
                except Exception as e:
                    msg = str(e)
                    if "404 Not Found" in msg:
                        errors.append((steam, "doesn’t have a Leetify profile"))
                    else:
                        errors.append((steam, msg))

            await session.commit()

        scope_txt = "all guilds" if all_guilds else "this server"
        msg = f"Backfill complete. Ingested matches for {total} unique Steam IDs ({scope_txt})."
        if errors:
            msg += "\n" + f"{len(errors)} failed:\n" + "\n".join(f"- steam `{s}`: {err}" for s, err in errors[:6])
        await interaction.followup.send(msg, ephemeral=True)

    @stats.command(name="update_all", description="Update player_stats for all registered users this week")
    @app_commands.describe(all_guilds="If true, process users from EVERY guild (use sparingly)")
    @app_commands.checks.has_permissions(administrator=True)
    async def update_all(self, interaction: discord.Interaction, limit: int = 100, all_guilds: bool = False):
        """
            Admin Only

            Rebuilds PlayerStats table for all registered users this week (broader than update_stats)
            Writes:
                Matches & Player game (refreshed by ingest_user_recent_matches)
                PlayerStats updated/inserted via upsert stats
                Does directly update WeeklyPoints ADDED


            Use this for a system wide refresh (not limited to people in teams like 'update_stats' command

        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        scope_guild_id = interaction.guild_id
        if not scope_guild_id and not all_guilds:
            await interaction.followup.send("Use this in a server (or pass all_guilds=True).", ephemeral=True)
            return

        # canonical week boundary — use this SAME key everywhere
        week_start_utc_naive, week_end_utc_naive = week_bounds_naive_utc("Europe/London")
        week_label = week_start_utc_naive.strftime("%d-%b-%Y")

        updated = 0
        skipped_no_games: list[int] = []  # discord_ids with no games this week
        failed: list[tuple[int, str]] = []  # (discord_id or steam, error)
        print(f' Week Start: {week_start_utc_naive}')
        print(f' Week End: {week_end_utc_naive}')
        print(f' Week Label: {week_label}')

        async with SessionLocal() as session:
            # pick users in-scope
            q = select(User.id, User.discord_id, User.steam_id, User.discord_guild_id).where(
                User.steam_id.is_not(None)
            )
            if not all_guilds:
                q = q.where(User.discord_guild_id == scope_guild_id)

            users = (await session.execute(q)).all()
            if not users:
                scope = "all guilds" if all_guilds else f"this server ({scope_guild_id})"
                await interaction.followup.send(f"No registered users with Steam IDs in {scope}.", ephemeral=True)
                return

            #ingest once per unique steam
            unique_steams = {int(s) for _, _, s, _ in users if s is not None}
            for steam in unique_steams:
                try:
                    await ingest_user_recent_matches(session, steam_id=steam, limit=limit)
                except Exception as e:
                    # keep going; we can still aggregate what we have
                    failed.append((steam, f"ingest: {e}"))

            #aggregate per user/guild and upsert
            for uid, did, steam, user_guild_id in users:
                try:
                    if steam is None:
                        failed.append((did, "no Steam linked"))
                        continue

                    breakdown = await aggregate_week_from_db(
                        session,
                        steam_id=int(steam),
                        week_start_utc=week_start_utc_naive,
                        week_end_utc=week_end_utc_naive,
                    )

                    sample = int(breakdown.get("sample_size") or 0)
                    if sample == 0:
                        skipped_no_games.append(did)
                        continue

                    mm_g = int(breakdown.get("mm_games", 0) or 0)
                    fac_g = int(breakdown.get("faceit_games", 0) or 0)
                    prem_g = int(breakdown.get("premier_games", 0) or 0)
                    ren_g = int(breakdown.get("renown_games", 0) or 0)
                    other_games = max(0, sample - (mm_g + fac_g + prem_g + ren_g))

                    await upsert_stats(
                        session=session,
                        user_id=uid,
                        guild_id=user_guild_id,
                        avg_leetify_rating=breakdown.get("avg_leetify_rating"),
                        sample_size=sample,
                        trade_kills=breakdown.get("trade_kills"),
                        ct_rating=breakdown.get("ct_rating"),
                        t_rating=breakdown.get("t_rating"),
                        adr=breakdown.get("adr"),
                        entries=breakdown.get("entries", 0.0),
                        flashes=breakdown.get("flashes"),
                        util_dmg=breakdown.get("util_dmg"),
                        faceit_games=fac_g,
                        premier_games=prem_g,
                        renown_games=ren_g,
                        mm_games=mm_g,
                        other_games=other_games,
                        wins=breakdown.get("wins"),
                    )

                    bd = breakdown_from_agg(breakdown)
                    await upsert_weekly_points_from_breakdown(
                        session,
                        week_start_utc=week_start_utc_naive,  # exact canonical key; no offsets
                        guild_id=user_guild_id,
                        user_id=uid,
                        ruleset_id=1,
                        bd=bd,
                    )
                    updated += 1

                except Exception as e:
                    msg = str(e)
                    if "404 Not Found" in msg:
                        failed.append((did, "doesn’t have a Leetify profile"))
                    else:
                        failed.append((did, msg))

            await session.commit()

        scope_txt = "all guilds" if all_guilds else f"this server ({scope_guild_id})"
        parts = [
            f"Updated player_stats & weekly_points for **{updated}** users (week starting {week_label}) in {scope_txt}."]
        if skipped_no_games:
            parts.append(f"Skipped (no games): {len(skipped_no_games)}")
            parts.extend(f"- <@{d}>" for d in skipped_no_games[:6])
        if failed:
            parts.append(f"Failed: {len(failed)}")
            for who, err in failed[:6]:
                parts.append(f"- {who}: {err}")

        await interaction.followup.send("\n".join(parts), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(stats(bot))