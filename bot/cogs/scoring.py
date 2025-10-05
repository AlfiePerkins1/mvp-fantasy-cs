# bot/cogs/scoring.py
import discord
from discord import app_commands
from discord.ext import commands


from typing import Optional
from datetime import datetime, timezone, timedelta


from sqlalchemy import select, func, delete, insert, case
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.db import SessionLocal
from backend.services.leetify_api import current_week_start_london
from backend.services.repo import get_or_create_user, upsert_stats
from backend.models import User, Team, Player, TeamPlayer, WeeklyPoints, PlayerGame, Match
from backend.services.ingest_user import ingest_user_recent_matches




MATCH_MULT = {"premier": 1.20, "faceit": 1.10, "renown": 1.00, "mm": 0.80}

def _v(x, d=0.0): return float(x) if x is not None else float(d)
def _i(x, d=0):   return int(x) if x is not None else int(d)

async def _guild_roster_db_user_ids(session, guild_id: int) -> list[int]:
    """
    Return DB user_ids for everyone on any team in this guild whose Player.handle is a digit (Discord ID).
    """
    rows = (await session.execute(
        select(Player.handle)
        .join(TeamPlayer, TeamPlayer.player_id == Player.id)
        .join(Team, Team.id == TeamPlayer.team_id)
        .where(Team.guild_id == guild_id)
    )).scalars().all()

    user_ids: list[int] = []
    seen: set[int] = set()
    for h in rows:
        text = str(h)
        if not text.isdigit():
            continue
        discord_id = int(text)
        user = await get_or_create_user(session, discord_id)
        if user.id not in seen:
            user_ids.append(user.id)
            seen.add(user.id)
    return user_ids

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

async def _guild_roster_targets(session, guild_id: int):
    """
    Returns list[(db_user_id, discord_id)] for everyone on any team in this guild
    whose Player.handle is a numeric Discord ID.
    """
    rows = (await session.execute(
        select(Player.handle)
        .join(TeamPlayer, TeamPlayer.player_id == Player.id)
        .join(Team, Team.id == TeamPlayer.team_id)
        .where(Team.guild_id == guild_id)
    )).scalars().all()

    targets = []
    seen = set()
    for h in rows:
        s = str(h)
        if not s.isdigit():
            continue
        discord_id = int(s)
        u = await get_or_create_user(session, discord_id)
        if u.id not in seen:
            seen.add(u.id)
            targets.append((u.id, discord_id))
    return targets


async def _aggregate_ps_from_db(session, *, user_id: int, week_start_utc: datetime) -> dict:
    """
    Aggregate the current week from PlayerGame and return the fields expected by upsert_stats().
    """
    PG = PlayerGame
    week_end_utc = week_start_utc + timedelta(days=7)

    (games,
     avg_lr, avg_ct, avg_t,
     trades,
     adr, flashes, util,
     wins,
     n_faceit, n_prem, n_ren, n_mm) = (await session.execute(
        select(
            func.count(PG.id),
            func.avg(PG.leetify_rating * 100.0),
            func.avg(PG.ct_leetify_rating * 100.0),
            func.avg(PG.t_leetify_rating * 100.0),
            func.sum(PG.trade_kills_succeed),

            func.avg(PG.dpr),
            func.sum(PG.flashbang_leading_to_kill),
            func.avg(PG.he_foes_damage_avg),

            func.sum(case((PG.won == True, 1), else_=0)),

            func.sum(case((PG.data_source == "faceit", 1), else_=0)),
            func.sum(case((PG.data_source == "matchmaking", 1), else_=0)),
            func.sum(case((PG.data_source == "renown", 1), else_=0)),
            func.sum(case((PG.data_source == "matchmaking_competitive", 1), else_=0)),
        )
        .where(
            PG.user_id == user_id,
            PG.finished_at >= week_start_utc,
            PG.finished_at <  week_start_utc + timedelta(days=7),
        )
    )).one()

    games_total  = int(games or 0)
    n_faceit     = int(n_faceit or 0)
    n_prem       = int(n_prem or 0)
    n_ren        = int(n_ren or 0)
    n_mm         = int(n_mm or 0)
    n_other      = max(0, games_total - (n_faceit + n_prem + n_ren + n_mm))

    return {
        "avg_leetify_rating": float(avg_lr) if avg_lr is not None else None,
        "ct_rating":          float(avg_ct) if avg_ct is not None else None,
        "t_rating":           float(avg_t)  if avg_t  is not None else None,
        "sample_size":        games_total,                         # use total games this week
        "trade_kills":        int(trades or 0),

        "adr":                float(adr) if adr is not None else None,
        "entries":            0.0,                                 # until API exposes it
        "flashes":            int(flashes or 0),
        "util_dmg":           float(util) if util is not None else None,

        "faceit_games":       n_faceit,
        "premier_games":      n_prem,
        "renown_games":       n_ren,
        "mm_games":           n_mm,
        "other_games":        n_other,
        "wins":               int(wins or 0),
    }


class Scoring(commands.Cog):
    """Scoring config & queries: view, set weights/multipliers, preview points."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Group: /scoring ...
    scoring = app_commands.Group(name="scoring", description="Configure or view scoring")


    @scoring.command(name="update_stats", description="(Admin) Recompute PlayerStats for this server (or one member) from match history")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="Only update this member (optional)", fetch="Also fetch new games first")
    async def update_stats(self, interaction: discord.Interaction, member: Optional[discord.User] = None, fetch: bool = True):
        """
            Admin Only
            Recomputes PlayerStats (and updates weekly fantasy points) for all players on a team, fetches recent matches

            Writes to:
                matches & Player Game (if fetch = true)
                PlayerStats, updated per guild with weekly averages and totals(LR, CTLR, TLR, ADR etc) via upsert stats
                WeeklyPoints

            Use to refresh weekly player stats and scores manually (I.E after new matches are played)

            Goal: Automate this so it just runs like every day at 00:00

        """
        if not interaction.guild_id:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild_id
        week_start_london = current_week_start_london()
        week_start_utc = week_start_london.astimezone(timezone.utc)

        async with SessionLocal() as session:
            async with session.begin():
                # build targets
                if member is not None:
                    u = await get_or_create_user(session, member.id)
                    targets = [(u.id, member.id)]
                else:
                    targets = await _guild_roster_targets(session, guild_id)

        if not targets:
            await interaction.followup.send("No roster members found with linked accounts.", ephemeral=True)
            return

        ingested = 0
        updated  = 0

        async with SessionLocal() as session:
            async with session.begin():
                for db_user_id, discord_id in targets:
                    # fetch new games into PlayerGame (idempotent)
                    if fetch:
                        try:
                            stats = await ingest_user_recent_matches(session, discord_id=discord_id, limit=100)
                            ingested += stats.get("inserted", 0)
                        except Exception as e:
                            print(f"[update_stats] ingest failed for {discord_id}: {e}")

                    # aggregate this week from PlayerGame
                    agg = await _aggregate_ps_from_db(session, user_id=db_user_id, week_start_utc=week_start_utc)

                    # upsert PlayerStats (per guild cache)
                    await upsert_stats(
                        session,
                        user_id=db_user_id,
                        guild_id=guild_id,
                        avg_leetify_rating=agg["avg_leetify_rating"],
                        sample_size=agg["sample_size"],
                        trade_kills=agg["trade_kills"],
                        ct_rating=agg["ct_rating"],
                        t_rating=agg["t_rating"],
                        adr=agg["adr"],
                        entries=agg["entries"],
                        flashes=agg["flashes"],
                        util_dmg=agg["util_dmg"],
                        faceit_games=agg["faceit_games"],
                        premier_games=agg["premier_games"],
                        renown_games=agg["renown_games"],
                        mm_games=agg["mm_games"],
                        other_games=agg["other_games"],
                        wins=agg["wins"],
                    )
                    updated += 1

                bd = breakdown_from_agg(agg)  # same scoring weights you use elsewhere
                await upsert_weekly_points_from_breakdown(
                    session,
                    week_start_utc=week_start_utc,  # must match what you read later
                    guild_id=guild_id,
                    user_id=db_user_id,
                    ruleset_id=1,
                    bd=bd,
                )

        who = f" for **{discord.utils.escape_markdown(member.display_name)}**" if member else ""
        fetched_msg = f" (fetched {ingested} new games)" if fetch else ""
        await interaction.followup.send(
            f"Updated PlayerStats for **{updated}** member(s){who}{fetched_msg}.",
            ephemeral=True
        )

    @scoring.command(name="update_all", description="Update player_stats for all registered users this week")
    @app_commands.checks.has_permissions(administrator=True)
    async def update_all(self, interaction: discord.Interaction, limit: int = 100):
        """
            Admin Only

            Rebuilds PlayerStats table for all registered users this week (broader than update_stats)
            Writes:
                Matches & Player game (refreshed by ingest_user_recent_matches)
                PlayerStats updated/inserted via upsert stats
                Does NOT directly update WeeklyPoints (LOOK INTO THIS MIGHT WANNA MAKE IT SO IT DOES)


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

        async with SessionLocal() as session:
            users = (await session.execute(
                select(User.id, User.discord_id, User.steam_id)
                .where(User.steam_id.is_not(None))
            )).all()

            for uid, did, steam in users:
                try:
                    #  make sure we have recent games
                    await ingest_user_recent_matches(session, discord_id=int(did), limit=limit)

                    #  aggregate this user's current week from player_games
                    breakdown = await aggregate_week_from_db(session, user_id=uid, week_start_utc=week_start)

                    #  compute other_games (anything not in the 4 buckets)
                    sample = breakdown.get("sample_size", 0) or 0
                    mm_g = breakdown.get("mm_games", 0) or 0
                    fac_g = breakdown.get("faceit_games", 0) or 0
                    prem_g = breakdown.get("premier_games", 0) or 0
                    ren_g = breakdown.get("renown_games", 0) or 0
                    other_games = max(0, int(sample) - int(mm_g + fac_g + prem_g + ren_g))

                    #  upsert into player_stats (ct_rating/t_rating not aggregated here → None)
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
                        week_start_utc=week_start_utc,
                        guild_id=guild_id,
                        user_id=uid,
                        ruleset_id=1,
                        bd=bd,
                    )

                    updated += 1
                except Exception as e:
                    failed.append((did, str(e)))
                    print(e)

            await session.commit()

        msg = f"Updated player_stats & weekly_points for {updated} users (week starting {week_start.date()})."
        if failed:
            msg += f"\n {len(failed)} failed:\n" + "\n".join(f"- <@{d}>: {err}" for d, err in failed[:6])
        await interaction.followup.send(msg, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Scoring(bot))
