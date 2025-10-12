import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_mentions

from typing import Optional
from backend.db import SessionLocal

from datetime import datetime, timedelta, timezone
from sqlalchemy import select, or_, func, and_
from sqlalchemy.sql import func as sqlfunc
from zoneinfo import ZoneInfo

from backend.models import User, WeeklyPoints
from backend.services.leaderboard import get_team_leaderboard
from backend.services.leetify_api import current_week_start_norm
from bot.cogs.stats_refresh import week_bounds_naive_utc



NO_PINGS = discord.AllowedMentions.none()

def _fmt_1dp(x) -> str:
    try:
        return f"{float(x):.1f}"
    except Exception:
        return "0.0"

async def _resolve_display_name_quick(guild, discord_id: int, fallback: str | None = None) -> str:
    """
    Fast, no-ping resolver for a Discord user’s display name.
    Tries cache first, then fetch; falls back to provided text.
    """
    m = guild.get_member(discord_id)
    if m:
        return m.display_name
    try:
        m = await guild.fetch_member(discord_id)
        if m:
            return m.display_name
    except Exception:
        pass
    return fallback or str(discord_id)




class Leaderboards(commands.Cog):
    """Scoring config & queries: view, set weights/multipliers, preview points."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    leaderboard = app_commands.Group(name="leaderboard", description="Configure or view leaderboard")

    @leaderboard.command(name="player", description="Show this week's players leaderboard")
    @app_commands.describe(
        limit="How many players to show (max 25)",
        all_guilds="Include players from ALL guilds (default: only this server)",
    )
    async def player(self, interaction: discord.Interaction, limit: int = 10, all_guilds: bool = False):
        guild = interaction.guild
        guild_id = interaction.guild_id
        if not guild_id and not all_guilds:
            await interaction.response.send_message("Use this in a server (or pass all_guilds=True).", ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)



        # same week key I already use
        week_norm = current_week_start_norm(datetime.now()) - timedelta(hours=1, minutes=1)
        week_start_utc_naive, week_end_utc_naive = week_bounds_naive_utc("Europe/London")
        print(f'Week norm: {week_norm}')
        print(f'Week Norm 2: {week_start_utc_naive}')


        async with SessionLocal() as session:
            async with session.begin():
                if not all_guilds:
                    # Latest row per *user* in THIS guild
                    latest = (
                        select(
                            WeeklyPoints.user_id,
                            func.max(WeeklyPoints.computed_at).label("latest_ts"),
                        )
                        .where(
                            WeeklyPoints.guild_id == guild_id,
                            WeeklyPoints.week_start == week_norm,
                            WeeklyPoints.weekly_score.isnot(None),
                            WeeklyPoints.computed_at.isnot(None),
                        )
                        .group_by(WeeklyPoints.user_id)
                    ).subquery()

                    q = (
                        select(
                            WeeklyPoints.user_id,
                            WeeklyPoints.weekly_score,
                            WeeklyPoints.sample_size,
                            User.discord_id,
                        )
                        .join(latest, and_(
                            WeeklyPoints.user_id == latest.c.user_id,
                            WeeklyPoints.computed_at == latest.c.latest_ts,
                        ))
                        .join(User, User.id == WeeklyPoints.user_id, isouter=True)
                        .order_by(WeeklyPoints.weekly_score.desc())
                        .limit(limit * 2)
                    )
                    raw = (await session.execute(q)).all()
                    rows = [(did, score, (games or 0)) for (_uid, score, games, did) in raw]

                else:
                    # ALL guilds: dedupe by *discord_id* (a user can exist in multiple guilds)
                    base = (
                        select(
                            User.discord_id.label("discord_id"),
                            WeeklyPoints.weekly_score.label("score"),
                            func.coalesce(WeeklyPoints.sample_size, 0).label("games"),
                            func.row_number().over(
                                partition_by=User.discord_id,
                                order_by=WeeklyPoints.computed_at.desc(),
                            ).label("rn"),
                        )
                        .join(User, User.id == WeeklyPoints.user_id)
                        .where(
                            WeeklyPoints.week_start == week_norm,
                            WeeklyPoints.weekly_score.isnot(None),
                            WeeklyPoints.computed_at.isnot(None),
                        )
                    ).subquery()

                    q = (
                        select(base.c.discord_id, base.c.score, base.c.games)
                        .where(base.c.rn == 1)
                        .order_by(base.c.score.desc())
                        .limit(limit * 2)
                    )
                    rows = (await session.execute(q)).all()  # [(discord_id, score)]

        if not rows:
            await interaction.followup.send(
                f"No scores for week starting {week_norm:%Y-%m-%d}.", allowed_mentions=NO_PINGS
            )
            return

        # Final safety dedupe by discord_id, then cap to limit
        seen, unique = set(), []
        for did, score, games in rows:
            if did is None or did in seen:
                continue
            seen.add(did)
            unique.append((did, score, int(games or 0)))
            if len(unique) >= limit:
                break

        def fmt_1dp(x) -> str:
            try:
                return f"{float(x):.1f}"
            except Exception:
                return "0.0"

        header = f"{'#':<2}  {'Player':<24} {'Score':>7} {'Games':>5}"
        sep = f"{'–' * 2}  {'–' * 24} {'–' * 7} {'–' * 5}"
        lines = ["```", header, sep]

        for rank, (discord_id, score, games) in enumerate(unique, start=1):
            name = await _resolve_display_name_quick(guild, int(discord_id), fallback=str(discord_id))
            name = "@" + escape_mentions(name)
            lines.append(f"{rank:<2}  {name[:24]:<24} {fmt_1dp(score):>7} {games:>5}")

        lines.append("```")

        scope_title = "All Guilds" if all_guilds else "Current Server"
        embed = discord.Embed(
            title=f"Leaderboard — {scope_title}",
            description="\n".join(lines),
            type="rich",
        )
        await interaction.followup.send(embed=embed, allowed_mentions=NO_PINGS)


    @leaderboard.command(name="teams", description="Show this week's team leaderboard")
    @app_commands.describe(limit="Number of teams to show (max 25)",
                           top="Also show top N contributors per team (0 to hide)")
    async def leaderboard_teams(self, interaction: discord.Interaction, limit: int = 10, top: int = 0):
        """
           Displays the top teams in the server based on this week's fantasy points.
           Reads WeeklyPoints for the current game week and sums by team,
           honoring TeamPlayer effective windows.
        """
        guild = interaction.guild
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this in a server (not DMs).", ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

        limit = max(1, min(int(limit), 25))
        top = max(0, min(int(top), 5))  # cap tiny to keep output readable

        # Using new function for getting week bounds 23:00 Sunday
        week_norm, week_end_utc_naive = week_bounds_naive_utc("Europe/London")

        limit = max(1, min(int(limit), 25))

        # Fetch aggregated leaderboard
        async with SessionLocal() as session:
            async with session.begin():
                data = await get_team_leaderboard(
                    session=session,
                    guild_id=guild_id,
                    week_start_norm=week_norm,
                    limit=limit,
                    offset=0,
                )

                # If showing top contributors, map internal user_id -> discord_id for mentions
                uid_to_discord = {}
                if top > 0 and data:
                    user_ids = {uid for row in data for (uid, _) in row["players"][:top]}
                    if user_ids:
                        res = await session.execute(
                            select(User.id, User.discord_id).where(User.id.in_(user_ids))
                        )
                        uid_to_discord = dict(res.tuples().all())

        if not data:
            ws_label = week_norm.strftime("%Y-%m-%d")
            await interaction.followup.send(
                f"No team scores for week starting {ws_label}.",
                allowed_mentions=NO_PINGS,
            )
            return

        # Build a monospace table like your players command
        header = f"{'#':<2}  {'Team':<24} {'Score':>7}  {'Owner':<10}"
        sep = f"{'–' * 2}  {'–' * 24} {'–' * 7}  {'–' * 10}"
        lines = ["```", header, sep]

        for rank, row in enumerate(data, start=1):
            team = row["team_name"][:24]
            score = _fmt_1dp(row["points"])
            if row.get("owner_discord_id"):
                owner_name = await _resolve_display_name_quick(guild, int(row["owner_discord_id"]),
                                                               fallback=str(row["owner_discord_id"]))
                owner_str = "@" + owner_name
            else:
                owner_str = "—"
            lines.append(f"{rank:<2}  {team:<24} {score:>7}  {owner_str[:24]:<10}")

            # Optional: small indented line with top N contributors
            if top > 0 and row["players"]:
                tops = []
                for (uid, pts) in row["players"][:top]:
                    did = uid_to_discord.get(uid)
                    if did:
                        name = await _resolve_display_name_quick(guild, int(did), fallback=str(did))
                        mentionish = "@" + name
                    else:
                        mentionish = f"User {uid}"
                    tops.append(f"{mentionish} {float(pts):.1f}")
                if tops:
                    lines.append(f"    Top: " + ", ".join(tops))

        lines.append("```")

        ws_label = week_norm.strftime("%Y-%m-%d")
        embed = discord.Embed(
            title=f"Team Leaderboard — Week starting {ws_label}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.followup.send(embed=embed, allowed_mentions=NO_PINGS)

async def setup(bot: commands.Bot):
    await bot.add_cog(Leaderboards(bot))