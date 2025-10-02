# bot/cogs/teams.py
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
from sqlalchemy import select, func
from discord.utils import escape_mentions

from backend.db import SessionLocal

from backend.services.repo import (get_or_create_user, create_team, add_player_by_handle, remove_player,
                                   ensure_player_for_user, get_cached_stats, is_stale, upsert_stats)

from backend.models import Team, TeamPlayer, Player

from backend.services.leetify_api import fetch_recent_matches, aggregate_player_stats, current_week_start_london

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
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message(
                "This command must be used in a server.", ephemeral=True
            )
            return

        norm_name = (name or "").strip()
        if not norm_name:
            await interaction.response.send_message("❗ Team name can’t be empty.", ephemeral=True)
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
            f"✅ Team created{f' **{norm_name}**' if norm_name else ''}!", ephemeral=True
        )

    @team.command(name="add", description=f"Add a player to your team (max {MAX_TEAM_SIZE})")
    async def add(self, interaction: discord.Interaction, member: discord.Member, role: Optional[str] = None):

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
            f"✅ Added {member.mention}{f' as **{role}**' if role else ''}.",
            ephemeral=True,
            allowed_mentions=NO_PINGS

        )

    @team.command(name="remove", description="Remove a player from your team")
    async def remove(self, interaction: discord.Interaction, member: discord.Member):

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
                    await interaction.response.send_message(f"{member.mention} isn't registered - they can't be in a team.", ephemeral=True)
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

        await interaction.response.send_message(f"Remove {member.mention} from the team", ephemeral=True, allowed_mentions=NO_PINGS
)

    @team.command(name="show", description="Show your current team")
    async def show(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this in a server (not DMs).", ephemeral=True)
            return

        target_user = user or interaction.user
        week_start = current_week_start_london()

        async with SessionLocal() as session:
            async with session.begin():
                target_db_user = await get_or_create_user(session, target_user.id)

                team = await session.scalar(
                    select(Team).where(Team.owner_id == target_db_user.id, Team.guild_id == guild_id)
                )
                if not team:
                    await interaction.response.send_message(
                        f"**{escape_mentions(target_user.display_name)}** has no team in this server.",
                        ephemeral=True,
                        allowed_mentions=NO_PINGS
                    )
                    return

                rows = (await session.execute(
                    select(Player.handle, TeamPlayer.role)
                    .join(TeamPlayer, TeamPlayer.player_id == Player.id)
                    .where(TeamPlayer.team_id == team.id)
                    .order_by(Player.handle.asc())
                )).all()

                # Build stat rows
                stat_rows = []  # (display, role, avg_txt)
                for handle, role in rows:
                    # Resolve handle to display name (if it's a Discord ID)
                    text = str(handle)
                    user_id_num = int(text) if text.isdigit() else None
                    if user_id_num:
                        display = await resolve_display_name(interaction.guild, user_id_num,
                                                             fallback=f"Unknown ({text})")
                    else:
                        display = text
                    display = escape_mentions(display)

                    # Default text if we can't compute
                    avg_txt = "n/a"
                    ct_txt = "n/a"
                    t_txt = "n/a"
                    trades_txt = "n/a"
                    games_txt = "n/a"

                    if user_id_num:
                        # DB lookup for the user whose stats we want
                        member_row = await get_or_create_user(session, user_id_num)

                        cached = await get_cached_stats(session, member_row.id, guild_id)
                        if is_stale(cached):
                            if member_row.steam_id:
                                try:
                                    matches = await fetch_recent_matches(member_row.steam_id, limit=20)
                                    agg = aggregate_player_stats(matches, member_row.steam_id, week_start_london=week_start)

                                    await upsert_stats(
                                        session,
                                        user_id=member_row.id,
                                        guild_id=guild_id,
                                        avg_leetify_rating=agg["avg_leetify_rating"],
                                        sample_size=agg["sample_size"],
                                        trade_kills=agg["trade_kills"],
                                        ct_rating=agg["ct_rating"],
                                        t_rating=agg["t_rating"],
                                    )
                                    if agg["avg_leetify_rating"] is not None:
                                        avg_txt = f"{agg['avg_leetify_rating']:.3f} ({agg['sample_size']})"
                                    if agg["ct_rating"] is not None:
                                        ct_txt = f"{agg['ct_rating']:.3f} ({agg['sample_size']})"
                                    if agg["t_rating"] is not None:
                                        t_txt = f"{agg['t_rating']:.3f} ({agg['sample_size']})"
                                    if agg["trade_kills"] is not None:
                                        trades_txt = f"{agg['trade_kills']:.3f} ({agg['sample_size']})"
                                except Exception:
                                    avg_txt = "n/a"
                            else:
                                avg_txt = "unregistered"
                        else:
                            if cached and cached.avg_leetify_rating is not None:
                                avg_txt = f"{cached.avg_leetify_rating:.3f}"
                            if cached and cached.ct_rating is not None:
                                ct_txt = f"{cached.ct_rating:.2f}"
                            if cached and cached.t_rating is not None:
                                t_txt = f"{cached.t_rating:.2f}"
                            if cached and cached.trade_kills is not None:
                                trades_txt = f"{int(cached.trade_kills):d}"
                            if cached and cached.sample_size is not None:
                                games_txt = f"{int(cached.sample_size or 0):d}"


                    stat_rows.append((display, role or "-", avg_txt, ct_txt, t_txt, trades_txt, games_txt))

        # Render table (monospace)
        def fmt_row(cols, widths):
            return " | ".join(str(c).ljust(w) for c, w in zip(cols, widths))

        headers = ["Player", "Role", "Avg", "CT", "T", "Trades", "Games"]
        widths = [
            max(len("Player"), max((len(r[0]) for r in stat_rows), default=6)),
            max(len("Role"), max((len(r[1]) for r in stat_rows), default=4)),
            max(len("Avg"), max((len(r[2]) for r in stat_rows), default=3)),
            max(len("CT"), max((len(r[3]) for r in stat_rows), default=2)),
            max(len("T"), max((len(r[4]) for r in stat_rows), default=1)),
            max(len("Trades"), max((len(r[5]) for r in stat_rows), default=6)),
            max(len("Games"), max((len(r[6]) for r in stat_rows), default=3)),
        ]

        table_lines = ["```", fmt_row(headers, widths), fmt_row(["-" * w for w in widths], widths)]
        for r in stat_rows:
            table_lines.append(fmt_row(r, widths))
        table_lines.append("```")

        embed = discord.Embed(
            title=f"{escape_mentions(target_user.display_name)}’s Team — {escape_mentions(team.name)}",
            type="rich"
        )
        embed.add_field(name="Players", value="\n".join(table_lines) if stat_rows else "_(no players yet)_",
                        inline=False)
        start_str = week_start.strftime("%Y-%m-%d %H:%M %Z")
        embed.set_footer(text=f"Leetify averages since {start_str} (London Time)")
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_PINGS)






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
