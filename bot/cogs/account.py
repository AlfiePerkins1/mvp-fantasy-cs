import discord
from discord import app_commands
from discord.ext import commands
from backend.db import SessionLocal
from backend.services.repo import set_user_steam_id, remove_user_steam_id, get_or_create_player, get_or_create_user
from discord.utils import escape_mentions


class Account(commands.Cog):
    """Account linking (Steam, Faceit, etc.)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    account = app_commands.Group(name="account", description="Link and manage your account")

    @account.command(name="register", description="Link your SteamID to use fantasy features")
    @app_commands.describe(steamid="Your SteamID(64 - bit E.G 76561198259409483)")
    async def register(self, interaction: discord.Interaction, steamid: str):
        """
            Links a discord user to their steam account

            Runs get_or_create_user to update the steamID for their sicrod ID.

            Use when first registering with the fantasy bot to link steam and be avaliable


        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        discord_id = interaction.user.id
        steamid = steamid.strip()

        async with SessionLocal() as session:
            async with session.begin():  # one transaction; auto-commits on success
                # make sure the user exists
                user = await get_or_create_user(session, discord_id=discord_id)
                # link/update steam id
                await set_user_steam_id(session, discord_id, steamid)
                # NEW: ensure there's a players row
                player, created = await get_or_create_player(session, discord_id)

        msg = "Registered! Your SteamID is linked."
        if created:
            msg += " You’ve been added to the player pool."
        await interaction.followup.send(msg, ephemeral=True)

    @account.command(name="unlink_user", description="(Admin) Unlink a member’s Steam account")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(member="Which member", purge_cached_stats="Also remove their cached stats for this server")
    async def unlink_user(
            self,
            interaction: discord.Interaction,
            member: discord.User,
            purge_cached_stats: bool = False,
    ):
        """
            Admin Only
            Runs remove_user_steam_id to do the opposite of account register

            Use to clean up if someone linked the wrong SteamID or left the server etc

        """

        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this in a server (not DMs).", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        async with SessionLocal() as session:
            async with session.begin():
                user, old = await remove_user_steam_id(
                    session,
                    discord_id=member.id,
                    purge_stats=purge_cached_stats,
                    guild_id=guild_id,
                )

        disp = escape_mentions(getattr(member, "display_name", str(member)))
        if old:
            msg = f"Unlinked **{disp}**’s Steam ID `{old}`."
        else:
            msg = f"**{disp}** had no Steam ID linked."

        if purge_cached_stats:
            msg += " Their cached stats for this server have been cleared."

        await interaction.followup.send(msg, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Account(bot))

