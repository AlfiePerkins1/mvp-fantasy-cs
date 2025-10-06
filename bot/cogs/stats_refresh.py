import discord
from discord import app_commands
from discord.ext import commands


from datetime import datetime, timezone, timedelta


from sqlalchemy import select, case, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.db import SessionLocal
from backend.services.leetify_api import current_week_start_london
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


async def aggregate_week_from_db(session, *, user_id: int, week_start_utc: datetime) -> dict:
    """Aggregate this user's week from PlayerGame."""
    PG = PlayerGame
    week_end_utc = week_start_utc + timedelta(days=7)

    res = (await session.execute(
        select(
            func.count(PG.id),                                    # games
            func.avg(PG.leetify_rating * 100.0),                  # avg rating 0..100
            func.sum(PG.trade_kills_succeed),                     # total trades
            func.avg(PG.dpr),                                     # ADR avg
            func.sum(PG.flashbang_leading_to_kill),               # flashes total
            func.avg(PG.he_foes_damage_avg),                      # util avg
            func.avg(PG.ct_leetify_rating * 100),
            func.avg(PG.t_leetify_rating * 100),

            func.sum(case((PG.won == True, 1), else_=0)),         # wins
            func.sum(case((PG.data_source == "matchmaking", 1), else_=0)),
            func.sum(case((PG.data_source == "faceit", 1), else_=0)),
            func.sum(case((PG.data_source == "renown", 1), else_=0)),
            func.sum(case((PG.data_source == "matchmaking_competitive", 1), else_=0)),
        )
        .where(
            PG.user_id == user_id,
            PG.finished_at >= week_start_utc,
            PG.finished_at <  week_end_utc,
        )
    )).one()

    (games, avg_rating, trades, adr, flashes, util,ct_rating, t_rating, wins, n_p, n_f, n_r, n_m) = res
    print(res)
    return {
        "sample_size": _i(games),
        "avg_leetify_rating": _v(avg_rating, None),
        "trade_kills": _i(trades),
        "adr": _v(adr, None),
        "flashes": _i(flashes),
        "util_dmg": _v(util, None),
        "ct_rating": _v(ct_rating, None),
        "t_rating": _v(t_rating, None),
        "wins": _i(wins),
        "premier_games": _i(n_p),
        "faceit_games": _i(n_f),
        "renown_games": _i(n_r),
        "mm_games": _i(n_m),
        "entries": 0.0,  # until API exposes it
    }

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
    @app_commands.checks.has_permissions(administrator=True)
    async def backfill_games(self, interaction: discord.Interaction, limit: int = 100):
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

        async with SessionLocal() as session:
            users = (await session.execute(
                select(User.discord_id).where(User.steam_id.is_not(None))
            )).scalars().all()

            total = 0
            errors = []
            for did in users:
                try:
                    await ingest_user_recent_matches(session, discord_id=int(did), limit=limit, guild_id=guild_id)
                    total += 1
                except Exception as e:
                    errors.append((did, str(e)))

            await session.commit()

        msg = f"Backfill complete. Ingested matches for {total} users."
        if errors:
            msg += f"\n{len(errors)} failed:\n" + "\n".join(f"- <@{d}>: {err}" for d, err in errors[:5])
        await interaction.followup.send(msg, ephemeral=True)

    @stats.command(name="update_all", description="Update player_stats for all registered users this week")
    @app_commands.checks.has_permissions(administrator=True)
    async def update_all(self, interaction: discord.Interaction, limit: int = 100):
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
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.followup.send("Use this in a server.", ephemeral=True)
            return

        week_start_london = current_week_start_london()
        week_start_utc = week_start_london.astimezone(timezone.utc)

        week_start = current_week_start_london()
        updated, failed = 0, []

        skipped_no_games = []

        async with SessionLocal() as session:
            users = (await session.execute(
                select(User.id, User.discord_id, User.steam_id)
                .where(User.steam_id.is_not(None))
            )).all()

            for uid, did, steam in users:
                try:
                    #  make sure we have recent games
                    await ingest_user_recent_matches(session, discord_id=int(did), limit=limit, guild_id=guild_id)
                    print("ingested")
                    #  aggregate this user's current week from player_games
                    breakdown = await aggregate_week_from_db(session, user_id=uid, week_start_utc=week_start)
                    print("broken down")
                    print(breakdown)
                    #  compute other_games (anything not in the 4 buckets)
                    sample = breakdown.get("sample_size", 0) or 0
                    mm_g = breakdown.get("mm_games", 0) or 0
                    fac_g = breakdown.get("faceit_games", 0) or 0
                    prem_g = breakdown.get("premier_games", 0) or 0
                    ren_g = breakdown.get("renown_games", 0) or 0
                    other_games = max(0, int(sample) - int(mm_g + fac_g + prem_g + ren_g))

                    #  Append discord id if there was no games played
                    print(sample)
                    if sample == 0:
                        skipped_no_games.append(did)
                        continue

                    print('upserting')
                    #  upsert into player_stats (ct_rating/t_rating not aggregated here  None)
                    await upsert_stats(
                        session=session,
                        user_id=uid,
                        guild_id=guild_id,
                        avg_leetify_rating=breakdown.get("avg_leetify_rating"),
                        sample_size=sample,
                        trade_kills=breakdown.get("trade_kills"),
                        ct_rating=breakdown.get("ct_rating"),
                        t_rating=breakdown.get("t_rating"),
                        adr=breakdown.get("adr"),
                        entries=breakdown.get("entries"),
                        flashes=breakdown.get("flashes"),
                        util_dmg=breakdown.get("util_dmg"),
                        faceit_games=fac_g,
                        premier_games=prem_g,
                        renown_games=ren_g,
                        mm_games=mm_g,
                        other_games=other_games,
                        wins=breakdown.get("wins"),
                    )

                    bd = breakdown_from_agg(breakdown)  # same weights as elsewhere
                    await upsert_weekly_points_from_breakdown(
                        session,
                        week_start_utc=week_start_utc + timedelta(hours=1),
                        guild_id=guild_id,
                        user_id=uid,
                        ruleset_id=1,
                        bd=bd,
                    )

                    updated += 1
                except Exception as e:
                    print(f'Error: {e}')
                    if "float() argument must be a string or a real number" in str(e) and "NoneType" in str(e):
                        failed.append('User(s) haven\'t played any games')
                    # failed.append((did, str(e)))
                    # print(e)

            await session.commit()

        msg = f"Updated player_stats & weekly_points for {updated} users (week starting {week_start.date()})."
        # New messages if people didnt play games
        if updated == 0 and not failed and skipped_no_games:
            msg = f"Updated player_stats & weekly_points for 0 users (week starting {week_start.date()}).\nLooks like no one played a game this week yet."
        elif skipped_no_games:
            msg += f"\n Skipped (no games): {len(skipped_no_games)}\n" + "\n".join(
                f"- <@{d}>" for d in skipped_no_games[:6])
        if failed:
            msg += f"\n Failed: {failed[:1]}"
            # msg += f"\n {len(failed)} failed:\n" + "\n".join(f"- <@{d}>: {err}" for d, err in failed[:6])
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(stats(bot))