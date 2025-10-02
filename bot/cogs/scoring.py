# bot/cogs/scoring.py
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

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

async def setup(bot: commands.Bot):
    await bot.add_cog(Scoring(bot))
