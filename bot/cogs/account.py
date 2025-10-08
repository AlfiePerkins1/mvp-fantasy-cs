import discord
from discord import app_commands
from discord.ext import commands
from discord.utils import escape_mentions

from backend.db import SessionLocal
from backend.services.repo import set_user_steam_id, remove_user_steam_id, get_or_create_player, get_or_create_user, create_user
from backend.services.leetify_api import leetify_profile_exists


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


                TODO:
                    Update to hold guild_id, so a user has different accounts across guilds

        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        member = interaction.user
        discord_id = member.id
        steamid = steamid.strip()
        guild_id = interaction.guild.id

        # grab readable names
        discord_username = member.name
        discord_global_name = getattr(member, "global_name", None)
        discord_display_name = member.display_name

        async with SessionLocal() as session:
            async with session.begin():
                user = await create_user(
                    session,
                    discord_id=discord_id,
                    discord_username=discord_username,
                    discord_global_name=discord_global_name,
                    discord_display_name=discord_display_name,
                    guild_id=guild_id
                )
                await set_user_steam_id(session, discord_id, steamid, guild_id=guild_id)
                player, created = await get_or_create_player(session, discord_id)

        exists = await leetify_profile_exists(steamid)

        if exists is not None:
            async with SessionLocal() as session:
                async with session.begin():
                    user = await create_user(session,
                                                    discord_id=discord_id,
                                                    guild_id=guild_id,
                                                    discord_username=discord_username,
                                                    discord_global_name=discord_global_name,
                                                    discord_display_name=discord_display_name
                                                    )
                    user.has_leetify = bool(exists)

        msg = f"Registered! Linked SteamID `{steamid}` for **{discord_display_name}**."
        if created:
            msg += " You’ve been added to the player pool."

        if exists is True:
            tail = " I can fetch your matches and compute stats normally."
        elif exists is False:
            tail = (
                " **Warning:** I couldn’t find a Leetify account for this SteamID "
                "(the API returned 404 Not Found). Your bot features will be *very* limited "
                "until you create one. You can sign up here: https://leetify.com/invite/b7b82917-06ce-49cd-b715-6f6b6b47212c"
                " If you believe this to be an error please create a new issue on the [GitHub Repository](https://github.com/AlfiePerkins1/mvp_fantasy_cs_issues)"
            )
        else:  # None (unknown / transient)
            tail = (
                " I couldn’t verify your Leetify profile right now (temporary error). "
                "I’ll try again later, or you can run `/scoring update_stats fetch:true` (Admin Only)."
            )

        await interaction.followup.send(f"{msg} {tail}", ephemeral=True)


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

