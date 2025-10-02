# bot/cogs/teams.py
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

from backend.db import SessionLocal
from backend.services.repo import get_or_create_user, create_team

MAX_TEAM_SIZE = 5  # tweak to 6 if you allow a sub

class Teams(commands.Cog):
    """Team management commands: create, add/remove players, view team, assign roles."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Group: /team ...
    team = app_commands.Group(name="team", description="Manage your fantasy team")

    @team.command(name="create", description="Create your fantasy team")
    async def create(self, interaction: discord.Interaction, name: Optional[str] = None):


        async with SessionLocal() as session:
            async with session.begin():
                user = await get_or_create_user(session, interaction.user.id)
                await create_team(session, user, name)
                print(f' Created Team for {user} with name {name} in session {session}')
        await interaction.response.send_message(
            f"✅ Team created{f' **{name}**' if name else ''}!", ephemeral=True
        )

    @team.command(name="add", description=f"Add a player to your team (max {MAX_TEAM_SIZE})")
    async def add(self, interaction: discord.Interaction, player: str, role: Optional[str] = None):
        # TODO: validate player exists; check team size; persist
        # role example values: star, entry, awper, support, igl
        await interaction.response.send_message(
            f"✅ Added **{player}**{f' as **{role}**' if role else ''}.", ephemeral=True
        )

    @team.command(name="remove", description="Remove a player from your team")
    async def remove(self, interaction: discord.Interaction, player: str):
        # TODO: remove from user team
        await interaction.response.send_message(f"❌ Removed **{player}**.", ephemeral=True)

    @team.command(name="show", description="Show your current team")
    async def show(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        target = user or interaction.user
        # TODO: fetch team for target.id
        # Example embed response:
        embed = discord.Embed(title=f"{target.display_name}'s Team", description="(placeholder)")
        # embed.add_field(name="Players", value="\n".join(["p1", "p2", "p3"]), inline=False)
        await interaction.response.send_message(embed=embed)

    @team.command(name="assign-role", description="Assign a role to a player on your team")
    async def assign_role(self, interaction: discord.Interaction, player: str, role: str):
        # TODO: validate role; update backend
        await interaction.response.send_message(
            f"🎯 **{player}** is now **{role}**.", ephemeral=True
        )

    @team.command(name="list-roles", description="List available player roles in scoring")
    async def list_roles(self, interaction: discord.Interaction):
        # TODO: fetch from backend; hardcode for now
        roles = ["star", "entry", "awper", "support", "igl"]
        await interaction.response.send_message("Available roles: " + ", ".join(roles), ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Teams(bot))
