import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional


class Leaderboard(commands.Cog):
    """Scoring config & queries: view, set weights/multipliers, preview points."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot



async def setup(bot: commands.Bot):
    await bot.add_cog(Leaderboard(bot))