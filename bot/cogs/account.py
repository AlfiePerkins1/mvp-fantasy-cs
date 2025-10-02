import discord
from discord import app_commands
from discord.ext import commands
from backend.db import SessionLocal
from backend.services.repo import set_user_steam_id

class Account(commands.Cog):
    """Account linking (Steam, Faceit, etc.)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    account = app_commands.Group(name="account", description="Link and manage your account")

    @account.command(name="register", description="Link your SteamID to use fantasy features")
    @app_commands.describe(steamid="Your SteamID(64 - bit E.G 76561198259409483)")
    async def register(self, interaction: discord.Interaction, steamid: str):
        async with SessionLocal() as session:
            async with session.begin():
                await set_user_steam_id(session, interaction.user.id, steamid.strip())

        await interaction.response.send_message(
            "✅ Registered! Your SteamID is linked to your Discord account.",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(Account(bot))

