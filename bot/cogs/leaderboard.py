import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_mentions

from backend.db import SessionLocal

from datetime import datetime, timedelta, timezone
from sqlalchemy import select

from backend.models import User, WeeklyPoints





NO_PINGS = discord.AllowedMentions.none()


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

    @leaderboard.command(name="show", description="Show this week's leaderboard")
    async def show(self, interaction: discord.Interaction, limit: int = 10):
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
                "No scores in the last 7 days. Run `/team show` for players, then `/team snapshot`.",
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


async def setup(bot: commands.Bot):
    await bot.add_cog(Leaderboards(bot))