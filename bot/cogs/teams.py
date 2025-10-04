# bot/cogs/teams.py
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
from sqlalchemy import select, func
from discord.utils import escape_mentions
from datetime import datetime, timedelta
from backend.db import SessionLocal

from backend.services.repo import get_or_create_user, create_team, ensure_player_for_user
from backend.models import Team, TeamPlayer, Player, WeeklyPoints, PlayerStats
from backend.services.leetify_api import current_week_start_london

MAX_TEAM_SIZE = 6  # 5 and a sub
NO_PINGS = discord.AllowedMentions.none()


async def resolve_display_name(guild: discord.Guild | None, user_id: int, fallback: str) -> str:
    if guild:
        m = guild.get_member(user_id)
        if m:
            return m.display_name
        try:
            m = await guild.fetch_member(user_id)
            return m.display_name
        except (discord.NotFound, discord.HTTPException, discord.Forbidden):
            pass
    return fallback


class Teams(commands.Cog):
    """Team management commands: create, add/remove players, view team, assign roles."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Group: /team ...
    team = app_commands.Group(name="team", description="Manage your fantasy team")

    @team.command(name="create", description="Create your fantasy team")
    async def create(self, interaction: discord.Interaction, name: Optional[str] = None):
        """
            Creates a new fantasy team for the user who runs command

            Writes:
                Team, inserts a new team record with the provided name and server ID

            Use to createa  fantasy team before adding members


        """

        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message(
                "This command must be used in a server.", ephemeral=True
            )
            return

        norm_name = (name or "").strip()
        if not norm_name:
            await interaction.response.send_message("Team name can’t be empty.", ephemeral=True)
            return

        # DB session must wrap all DB work
        async with SessionLocal() as session:
            async with session.begin():
                # fetch or create the user row
                user = await get_or_create_user(session, interaction.user.id)

                # check if team already exists for this user in this guild
                existing = await session.scalar(
                    select(Team).where(
                        Team.owner_id == user.id,
                        Team.guild_id == guild_id
                    )
                )
                if existing:
                    await interaction.response.send_message(
                        "⚠️ You already have a team in this server.",
                        ephemeral=True
                    )
                    return

                # create the new team
                await create_team(session, user, norm_name, guild_id)

        await interaction.response.send_message(
            f"Team created{f' **{norm_name}**' if norm_name else ''}!", ephemeral=True
        )

    @team.command(name="add", description=f"Add a player to your team (max {MAX_TEAM_SIZE})")
    async def add(self, interaction: discord.Interaction, member: discord.Member, role: Optional[str] = None):

        """
            Adds registered player (disc user) to the callers team and optionall assigns a role (ToDo)
            Writes:
                Player (ensure row exsits 'ensure_player_for_user')
                TeamPlayer, inserts a new link between team and player

            Use to build a fantasy team by adding members

        """

        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return

        async with SessionLocal() as session:
            async with session.begin():
                # caller user row (owner of the team)
                owner = await get_or_create_user(session, interaction.user.id)

                #  enforce the caller has a team in THIS guild
                team = await session.scalar(
                    select(Team).where(Team.owner_id == owner.id, Team.guild_id == guild_id)
                )
                if not team:
                    await interaction.response.send_message("Create a team first: `/team create`", ephemeral=True)
                    return

                #  target user must be registered
                target_user = await get_or_create_user(session, member.id)
                if not target_user.steam_id:
                    await interaction.response.send_message(
                        f"❗ {member.mention} isn’t registered. Ask them to run `/account register <steamid>` first.",
                        ephemeral=True,
                        allowed_mentions=NO_PINGS

                    )
                    return

                #  enforce max size
                current_size = await session.scalar(
                    select(func.count()).select_from(TeamPlayer).where(TeamPlayer.team_id == team.id)
                )
                if current_size >= MAX_TEAM_SIZE:
                    await interaction.response.send_message(f"Team is full (max {MAX_TEAM_SIZE}).", ephemeral=True)
                    return

                #  ensure a Player row exists for this registered user (optional, if you keep Player table)
                player_row = await ensure_player_for_user(session, target_user)

                #  link to team (role optional)
                tp = TeamPlayer(team_id=team.id, player_id=player_row.id, role=role)
                session.add(tp)
                await session.flush()

        await interaction.response.send_message(
            f" Added {member.mention}{f' as **{role}**' if role else ''}.",
            ephemeral=True,
            allowed_mentions=NO_PINGS

        )

    @team.command(name="remove", description="Remove a player from your team")
    async def remove(self, interaction: discord.Interaction, member: discord.Member):
        """
            Removes a player from the callers team in the current guild

            Writes:
                TeamPlayer (deletes row linking the team to the player)

            Use to kick players from the callers team

        """

        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return

        async with SessionLocal() as session:
            async with session.begin():
                # Caller (owner of team)
                owner = await get_or_create_user(session, interaction.user.id)

                # Make sure the owner has a team in the guild
                team = await session.scalar(
                    select(Team).where(Team.owner_id == owner.id, Team.guild_id == guild_id)
                )
                if not team:
                    await interaction.response.send_message("You don't have a team in this server.", ephemeral=True)
                    return

                target_user = await get_or_create_user(session, member.id)
                if not target_user.steam_id:
                    await interaction.response.send_message(
                        f"{member.mention} isn't registered - they can't be in a team.", ephemeral=True)
                    return

                player_row = await ensure_player_for_user(session, target_user)

                tp = await session.scalar(
                    select(TeamPlayer).where(
                        TeamPlayer.team_id == team.id,
                        TeamPlayer.player_id == player_row.id,
                    )
                )
                if not tp:
                    await interaction.response.send_message(
                        f"{member.mention} isn’t on your team.",
                        ephemeral=True,
                        allowed_mentions=NO_PINGS

                    )
                    return

                await session.delete(tp)

        await interaction.response.send_message(f"Remove {member.mention} from the team", ephemeral=True,
                                                allowed_mentions=NO_PINGS
                                                )

    ### FIX IT ###
    @team.command(name="show", description="Show your current team")
    async def show(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        """
            Shows the users fantasy team with each players weekly score (and a team total)
            Doesnt write only reads

            Use to see current rosters weekly totals and the total team score


        """

        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this in a server (not DMs).", ephemeral=True)
            return

        target_user = user or interaction.user
        week_start = current_week_start_london()
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

        # --- Small helper functions ---
        def fmt_2sf(x) -> str:
            try:
                return f"{float(x):.2g}"
            except:
                return "n/a"

        def fmt_1dp(x) -> str:
            try:
                return f"{float(x):.1f}"
            except:
                return "n/a"

        # --- Fetch team info ---
        async with SessionLocal() as session:
            async with session.begin():
                # Find the owner’s DB user row
                target_db_user = await get_or_create_user(session, target_user.id)

                # Get their team (if any)
                team_row = await session.execute(
                    select(Team.id, Team.name)
                    .where(Team.owner_id == target_db_user.id, Team.guild_id == guild_id)
                )
                team_data = team_row.first()
                if not team_data:
                    await interaction.followup.send(
                        f"**{escape_mentions(target_user.display_name)}** has no team in this server.",
                        allowed_mentions=NO_PINGS
                    )
                    return

                team_id, team_name = team_data

                # Get all players (handles + roles)
                roster = await session.execute(
                    select(Player.id, Player.handle, TeamPlayer.role)
                    .join(TeamPlayer, TeamPlayer.player_id == Player.id)
                    .where(TeamPlayer.team_id == team_id)
                    .order_by(Player.handle.asc())
                )
                roster = roster.all()

                if not roster:
                    await interaction.followup.send(
                        f"**{escape_mentions(target_user.display_name)}**’s team **{escape_mentions(team_name)}** has no players yet.",
                        allowed_mentions=NO_PINGS
                    )
                    return

                # Build player display info
                stat_rows = []
                for player_id, handle, role in roster:
                    # Find associated DB user for this handle (handle = discord_id as str)
                    user_id_num = int(handle) if str(handle).isdigit() else None
                    display_name = f"Unknown ({handle})"
                    avg_txt = score_txt = games_txt = "n/a"

                    if user_id_num:
                        member_row = await get_or_create_user(session, user_id_num)
                        db_user_id = member_row.id

                        # Try to get display name from Discord
                        display_name = await resolve_display_name(
                            interaction.guild, user_id_num, fallback=f"Unknown ({handle})"
                        )
                        display_name = escape_mentions(display_name)

                        # Read from PlayerStats (cached averages)
                        stats_row = await session.scalar(
                            select(PlayerStats)
                            .where(PlayerStats.user_id == db_user_id, PlayerStats.guild_id == guild_id)
                        )
                        if stats_row:
                            if stats_row.avg_leetify_rating is not None:
                                avg_txt = fmt_2sf(stats_row.avg_leetify_rating)
                            games_txt = str(stats_row.sample_size or 0)

                        # Read weekly fantasy points (from WeeklyPoints)
                        points_row = await session.scalar(
                            select(WeeklyPoints.weekly_score)
                            .where(
                                WeeklyPoints.user_id == db_user_id,
                                WeeklyPoints.guild_id == guild_id,
                                WeeklyPoints.week_start >= week_start - timedelta(days=7)
                            )
                            .order_by(WeeklyPoints.week_start.desc())
                        )
                        if points_row is not None:
                            score_txt = fmt_1dp(points_row)

                    stat_rows.append((display_name, role or "-", avg_txt, score_txt, games_txt))

        # --- Build table for display ---
        def fmt_row(cols, widths):
            return " | ".join(str(c).ljust(w) for c, w in zip(cols, widths))

        headers = ["Player", "Role", "Avg", "Score", "Games"]
        widths = [
            max(len("Player"), max((len(r[0]) for r in stat_rows), default=6)),
            max(len("Role"), max((len(r[1]) for r in stat_rows), default=4)),
            max(len("Avg"), max((len(r[2]) for r in stat_rows), default=3)),
            max(len("Score"), max((len(r[3]) for r in stat_rows), default=5)),
            max(len("Games"), max((len(r[4]) for r in stat_rows), default=3)),
        ]

        lines = ["```", fmt_row(headers, widths), fmt_row(["-" * w for w in widths], widths)]
        for r in stat_rows:
            lines.append(fmt_row(r, widths))
        lines.append("```")

        embed = discord.Embed(
            title=f"{escape_mentions(target_user.display_name)}’s Team — {escape_mentions(team_name)}",
            type="rich"
        )
        embed.add_field(
            name="Players",
            value="\n".join(lines) if stat_rows else "_(no players yet)_",
            inline=False
        )

        start_str = week_start.strftime("%Y-%m-%d %H:%M %Z")
        embed.set_footer(text=f"Fantasy points from WeeklyPoints since {start_str} (London Time)")

        await interaction.followup.send(embed=embed, allowed_mentions=NO_PINGS)


async def setup(bot: commands.Bot):
    await bot.add_cog(Teams(bot))
