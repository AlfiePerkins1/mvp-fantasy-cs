import discord
from discord import app_commands
from discord.ext import commands

from backend.services.leetify_api import current_week_start_london

from backend.db import SessionLocal

from discord.utils import escape_mentions

from backend.models import User, Team, Player, TeamPlayer, ScoringConfig, PlayerStats, WeeklyPoints



from sqlalchemy import select


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

        guild = interaction.guild
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this in a server (not DMs).", ephemeral=True)
            return

        # Defer to avoid 3s timeouts
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

        week_start = current_week_start_london().date()
        limit = max(1, min(int(limit), 25))  # clamp 1..25

        # Pull top N from weekly_points
        async with SessionLocal() as session:
            async with session.begin():
                rows = (await session.execute(
                    select(
                        WeeklyPoints.user_id,
                        WeeklyPoints.weekly_score,
                        WeeklyPoints.sample_size,
                        User.discord_id,
                    )
                    .join(User, User.id == WeeklyPoints.user_id)
                    .where(
                        WeeklyPoints.week_start == week_start,
                        WeeklyPoints.guild_id == guild_id,
                        WeeklyPoints.sample_size > 0,
                    )
                    .order_by(WeeklyPoints.weekly_score.desc())
                    .limit(limit)
                )).all()

        if not rows:
            await interaction.followup.send(
                "No scores for this week yet. Run `/team snapshot` (or `/team show`) to generate them.",
                allowed_mentions=NO_PINGS,
            )
            return

        # Render
        def fmt_1dp(x) -> str:
            try:
                return f"{float(x):.1f}"
            except Exception:
                return "0.0"

        header = f"{'#':<2}  {'Player':<22} {'Score':>7} {'Games':>5}"
        sep = f"{'–' * 2}  {'–' * 22} {'–' * 7} {'–' * 5}"
        lines = ["```", header, sep]

        for rank, (user_id, score, games, discord_id) in enumerate(rows, start=1):
            # Resolve a display name from Discord ID (no pings)
            if discord_id:
                name = await _resolve_display_name_quick(guild, int(discord_id), fallback=str(discord_id))
            else:
                name = f"User {user_id}"
            name = escape_mentions(name)[:22]
            lines.append(f"{rank:<2}  {name:<22} {fmt_1dp(score):>7} {int(games):>5}")

        lines.append("```")

        embed = discord.Embed(
            title=f"Leaderboard — Week of {week_start.isoformat()}",
            description="\n".join(lines),
            type="rich",
        )
        await interaction.followup.send(embed=embed, allowed_mentions=NO_PINGS)


async def setup(bot: commands.Bot):
    await bot.add_cog(Leaderboards(bot))