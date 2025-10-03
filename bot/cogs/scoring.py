# bot/cogs/scoring.py
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
from backend.services.leetify_api import current_week_start_london

from backend.db import SessionLocal

from discord.utils import escape_mentions
from sqlalchemy import select, func, delete, insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from datetime import datetime, timezone, timedelta

from backend.services.repo import get_or_create_user
from backend.services.scoring import make_breakdown
from backend.models import User, Team, Player, TeamPlayer, ScoringConfig, PlayerStats, WeeklyPoints

class Scoring(commands.Cog):
    """Scoring config & queries: view, set weights/multipliers, preview points."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Group: /scoring ...
    scoring = app_commands.Group(name="scoring", description="Configure or view scoring")

    @scoring.command(name="show", description="Show current scoring configuration")
    async def show(self, interaction: discord.Interaction):
        # TODO: fetch from backend
        # Example placeholder:
        config = {
            "kill": 1.0,
            "assist": 0.5,
            "death": -0.5,
            "entry_kill": 2.0,
            "win_bonus_per_5": 10,
            "role_multipliers": {"star": 1.5, "entry": 2.0, "awper": 2.0, "support": 1.2, "igl": 1.1},
        }
        embed = discord.Embed(title="Fantasy Scoring")
        for k, v in config.items():
            if isinstance(v, dict):
                embed.add_field(name=k, value="\n".join(f"- {rk}: {rv}x" for rk, rv in v.items()), inline=False)
            else:
                embed.add_field(name=k, value=str(v))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @scoring.command(name="set-weight", description="Set base weight for an event (e.g., kill, assist)")
    @app_commands.describe(event="Event name, e.g., kill, assist, death, entry_kill", weight="Numeric weight")
    async def set_weight(self, interaction: discord.Interaction, event: str, weight: float):
        # TODO: persist change in backend
        await interaction.response.send_message(
            f"⚙️ Set **{event}** weight to **{weight}**.", ephemeral=True
        )

    @scoring.command(name="set-role-multiplier", description="Set multiplier for a role")
    @app_commands.describe(role="Role name (star, entry, awper, support, igl)", multiplier="e.g., 1.5")
    async def set_role_multiplier(self, interaction: discord.Interaction, role: str, multiplier: float):
        # TODO: persist change
        await interaction.response.send_message(
            f"🧮 Set **{role}** multiplier to **{multiplier}x**.", ephemeral=True
        )

    @scoring.command(name="set-win-bonus", description="Set bonus points for every N wins")
    @app_commands.describe(every_n_wins="e.g., 5", bonus_points="e.g., 10")
    async def set_win_bonus(self, interaction: discord.Interaction, every_n_wins: int, bonus_points: int):
        # TODO: persist change
        await interaction.response.send_message(
            f"🏆 Set win bonus: **+{bonus_points}** points per **{every_n_wins}** wins.", ephemeral=True
        )

    @scoring.command(name="preview", description="Preview points for a player given sample stats")
    @app_commands.describe(player="Player name/ID", kills="Kills", assists="Assists", deaths="Deaths", entry_kills="Entry kills")
    async def preview(
        self,
        interaction: discord.Interaction,
        player: str,
        kills: int,
        assists: int,
        deaths: int,
        entry_kills: Optional[int] = 0,
    ):
        # TODO: fetch weights/multipliers & compute via backend
        # Placeholder simple calc:
        base = kills * 1.0 + assists * 0.5 + deaths * -0.5 + (entry_kills or 0) * 2.0
        await interaction.response.send_message(
            f"📈 Preview for **{player}**: **{base:.1f}** points (placeholder).", ephemeral=True
        )

    @scoring.command(name="snapshot", description="(Admin) Recompute weekly points for this server or a specific member")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="Recompute only for this member (optional)")
    async def snapshot(self, interaction: discord.Interaction, member: Optional[discord.User] = None):
        if not interaction.guild_id:
            await interaction.response.send_message("Run this in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild_id
        week_start = current_week_start_london().date()
        ruleset_id = 1  # swap to your active ruleset if you version rules

        updated = 0
        skipped = 0

        async with SessionLocal() as session:
            async with session.begin():
                # scope which PlayerStats rows we’ll snapshot
                q = select(PlayerStats).where(
                    PlayerStats.guild_id == guild_id,
                    PlayerStats.sample_size.isnot(None),
                    PlayerStats.sample_size > 0,
                )
                if member is not None:
                    # limit to one user
                    db_user = await get_or_create_user(session, member.id)
                    q = q.where(PlayerStats.user_id == db_user.id)

                rows = (await session.scalars(q)).all()

                for ps in rows:
                    bd = make_breakdown(ps)

                    # Build a single insert ... on conflict do update
                    stmt = sqlite_insert(WeeklyPoints).values(
                        week_start=week_start,
                        guild_id=guild_id,
                        user_id=ps.user_id,
                        ruleset_id=ruleset_id,
                        computed_at=datetime.now(timezone.utc),
                        **bd,
                    )
                    update_cols = {k: getattr(stmt.excluded, k) for k in bd.keys()}
                    update_cols["computed_at"] = datetime.now(timezone.utc)
                    update_cols["ruleset_id"] = ruleset_id

                    stmt = stmt.on_conflict_do_update(
                        index_elements=["week_start", "guild_id", "user_id"],
                        set_=update_cols,
                    )
                    await session.execute(stmt)
                    updated += 1

        if updated == 0:
            msg = "No PlayerStats with games found to snapshot."
        else:
            who = f" for **{escape_mentions(member.display_name)}**" if member else ""
            msg = f"Recomputed **{updated}** weekly snapshot(s){who}."

        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Scoring(bot))
