import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_mentions

from typing import Optional
from backend.db import SessionLocal

from datetime import datetime, timedelta, timezone
from sqlalchemy import select

from backend.models import User, WeeklyPoints
from backend.services.leaderboard import get_team_leaderboard
from backend.services.leetify_api import current_week_start_norm




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
    async def player(self, interaction: discord.Interaction, limit: int = 10):
        """
            Displays the top players in the server based on fantasy points scored in the last 7 days.
            Reads Weekly_points table and filters rows so the week start is >= today - 7 days, the players are in this
            server and not another, and weekly score isnt null

            Use to check the current individual fantasy leaderboard for the past week.

        """

        guild = interaction.guild
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this in a server (not DMs).", ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

        # last 7 days window (robust against DATE/DATETIME mismatch)
        since_dt = datetime.now(timezone.utc) - timedelta(days=7)
        limit = max(1, min(int(limit), 25))

        # pull top N — LEFT JOIN so a missing User row doesn't drop the score
        async with SessionLocal() as session:
            async with session.begin():
                rows = (await session.execute(
                    select(
                        WeeklyPoints.user_id,
                        WeeklyPoints.weekly_score,
                        User.discord_id,
                    )
                    .join(User, User.id == WeeklyPoints.user_id, isouter=True)
                    .where(
                        WeeklyPoints.guild_id == guild_id,
                        WeeklyPoints.week_start >= since_dt,
                        WeeklyPoints.weekly_score.isnot(None),
                    )
                    .order_by(WeeklyPoints.weekly_score.desc())
                    .limit(limit)
                )).all()

        if not rows:
            await interaction.followup.send(
               f"No scores since {since_dt} days. Run `\\pricing backfill_games` and `\\scoring update_all` (Admin Only) ",
                allowed_mentions=NO_PINGS,
            )
            return

        def fmt_1dp(x) -> str:
            try:
                return f"{float(x):.1f}"
            except Exception:
                return "0.0"

        header = f"{'#':<2}  {'Player':<24} {'Score':>7}"
        sep = f"{'–' * 2}  {'–' * 24} {'–' * 7}"
        lines = ["```", header, sep]

        for rank, (user_id, score, discord_id) in enumerate(rows, start=1):
            if discord_id:
                name = await _resolve_display_name_quick(guild, int(discord_id), fallback=str(discord_id))
                name = "@" + escape_mentions(name)
            else:
                name = f"User {user_id}"
            lines.append(f"{rank:<2}  {name[:24]:<24} {fmt_1dp(score):>7}")

        lines.append("```")

        embed = discord.Embed(
            title="Leaderboard — Last 7 Days",
            description="\n".join(lines),
            type="rich",
        )
        await interaction.followup.send(embed=embed, allowed_mentions=NO_PINGS)

        async def build_embed(self, interaction: discord.Interaction) -> discord.Embed:
            offset = (self.page - 1) * self.limit
            async with self.bot.session_maker() as session:  # type: AsyncSession
                data = await get_team_leaderboard(
                    session=session,
                    guild_id=interaction.guild_id,
                    week_start_norm=self.week_norm,
                    limit=self.limit,
                    offset=offset,
                )

                # Map user_id -> discord_id for contributor mentions
                user_ids = {uid for row in data for (uid, _) in row["players"]}
                uid_to_discord = {}
                if user_ids:
                    res = await session.execute(
                        select(User.id, User.discord_id).where(User.id.in_(user_ids))
                    )
                    uid_to_discord = dict(res.tuples().all())

            ws_label = self.week_norm.strftime("%Y-%m-%d")
            embed = discord.Embed(
                title=f"Team Leaderboard — Week starting {ws_label}",
                color=discord.Color.gold(),
            )

            if not data:
                embed.description = "No teams found for this page."
                return embed

            lines = []
            rank_start = offset + 1
            for i, row in enumerate(data, start=rank_start):
                owner_tag = f" • Owner: <@{row['owner_discord_id']}>" if row.get("owner_discord_id") else ""
                lines.append(f"**{i}. {row['team_name']}** — {row['points']:.1f} pts{owner_tag}")

                # top 3 contributors
                if row["players"]:
                    top = []
                    for (uid, pts) in row["players"][:3]:
                        mention = f"<@{uid_to_discord.get(uid, 0)}>" if uid_to_discord.get(uid) else f"User {uid}"
                        top.append(f"{mention} {pts:.1f}")
                    lines.append("   · Top: " + ", ".join(top))

            embed.description = "\n".join(lines)
            return embed

        @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
        async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
            if self.page > 1:
                self.page -= 1
            embed = await self.build_embed(interaction)
            await interaction.response.edit_message(embed=embed, view=self)

        @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
        async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.page += 1
            embed = await self.build_embed(interaction)
            await interaction.response.edit_message(embed=embed, view=self)

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

        week_norm = current_week_start_norm(datetime.now())

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
        header = f"{'#':<2}  {'Team':<24} {'Score':>7}  {'Owner':<24}"
        sep = f"{'–' * 2}  {'–' * 24} {'–' * 7}  {'–' * 24}"
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
            lines.append(f"{rank:<2}  {team:<24} {score:>7}  {owner_str[:24]:<24}")

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