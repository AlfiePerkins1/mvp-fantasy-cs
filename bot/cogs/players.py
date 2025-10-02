import discord
from discord import app_commands
from discord.ext import commands

class Players(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    player = app_commands.Group(name="player", description="Player-related commands")

    @player.command(name="add")
    async def add(self, interaction: discord.Interaction, name: str):
        await interaction.response.send_message(f"✅ Added {name} to your team!")

    @player.command(name="remove")
    async def remove(self, interaction: discord.Interaction, name: str):
        await interaction.response.send_message(f"❌ Removed {name} from your team.")

    @player.command(name="info")
    async def info(self, interaction: discord.Interaction, name: str):
        await interaction.response.send_message(f"📊 Stats for {name}: ...")

async def setup(bot: commands.Bot):
    await bot.add_cog(Players(bot))
