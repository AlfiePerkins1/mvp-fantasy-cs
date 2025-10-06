# bot/cogs/teams.py
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
from sqlalchemy import select, func, or_
from discord.utils import escape_mentions
from datetime import timedelta, timezone, datetime
from backend.db import SessionLocal
from backend.services.market import already_on_team, roster_count, get_or_create_team_week_state, \
    get_global_player_price, TRANSFERS_PER_WEEK, buy_player, sell_player, team_has_active_this_week, roster_for_week

from backend.services.repo import get_or_create_user, create_team, ensure_player_for_user
from backend.models import Team, TeamPlayer, Player, WeeklyPoints, PlayerStats, player
from backend.services.leetify_api import current_week_start_london, next_week_start_london, current_week_start_norm, next_week_start_norm



MAX_TEAM_SIZE = 5  # 5 and a sub
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
                user = await get_or_create_user(session, interaction.user.id, discord_guild_id=guild_id)

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
        Queued add (effective next Monday 00:00). Enforces next-week cap always.
        Transfer limits are enforced ONLY after the first lock (i.e., when at least one player is active this_week).
        """
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        async with SessionLocal() as session:
            async with session.begin():
                owner = await get_or_create_user(session, interaction.user.id, discord_guild_id=guild_id)
                team = await session.scalar(select(Team).where(Team.owner_id == owner.id, Team.guild_id == guild_id))
                if not team:
                    await interaction.followup.send("Create a team first: `/team create`", ephemeral=True)
                    return

                target_user = await get_or_create_user(session, member.id, discord_guild_id=guild_id)
                if not target_user.steam_id:
                    await interaction.followup.send(
                        f"{member.mention} isn’t registered. Ask them to run `/account register <steamid>` first.",
                        ephemeral=True, allowed_mentions=NO_PINGS
                    );
                    return

                player_row = await ensure_player_for_user(session, target_user)

                now = datetime.now(tz=timezone.utc)
                this_week = current_week_start_norm(now)
                next_week = next_week_start_norm(now)

                state = await get_or_create_team_week_state(session, guild_id, team.id, this_week)

                price = await get_global_player_price(session, player_row.id)
                if price is None:
                    await interaction.followup.send(
                        "No price available for this player. Ask an admin to run `/pricing update`.", ephemeral=True)
                    return

                # Cap + dupes are always enforced against NEXT WEEK (because add takes effect next_week)
                next_ids_before = set(await roster_for_week(session, team.id, next_week))
                if player_row.id in next_ids_before:
                    await interaction.followup.send(f"{member.mention} is already queued/active for next week.",
                                                    ephemeral=True);
                    return
                if len(next_ids_before) >= MAX_TEAM_SIZE:
                    await interaction.followup.send(f"Your team is full for next week (max {MAX_TEAM_SIZE}).",
                                                    ephemeral=True);
                    return
                if state.budget_remaining < price:
                    await interaction.followup.send(
                        f"Not enough budget. Price: {price}, remaining: {state.budget_remaining}.", ephemeral=True
                    );
                    return

                # Only enforce transfer LIMITS after the first lock
                post_first_lock = await team_has_active_this_week(session, team.id, this_week)
                if post_first_lock and TRANSFERS_PER_WEEK and state.transfers_used >= TRANSFERS_PER_WEEK:
                    await interaction.followup.send(
                        f"You’ve already used your {TRANSFERS_PER_WEEK} transfer(s) this week.", ephemeral=True
                    );
                    return

                # Insert queued add (effective next_week)
                session.add(TeamPlayer(
                    team_id=team.id,
                    player_id=player_row.id,
                    role=role,
                    effective_from_week=next_week,
                    effective_to_week=None
                ))
                await session.flush()

                # Budget now (FPL-style)
                state.budget_remaining -= price

                # Only COUNT a transfer after the first lock
                transfers_used = state.transfers_used
                if post_first_lock:
                    this_ids = set(await roster_for_week(session, team.id, this_week))
                    if player_row.id not in this_ids:
                        state.transfers_used += 1
                        transfers_used = state.transfers_used

                # Optional: mark build_complete when next-week roster hits cap (cosmetic/UX)
                if not team.build_complete and (len(next_ids_before) + 1) >= MAX_TEAM_SIZE:
                    team.build_complete = True

        await interaction.followup.send(
            f"Added {member.mention}{f' as **{role}**' if role else ''}. "
            f"Price: **{price}**. Budget remaining: **{state.budget_remaining}**. "
            f"Transfers this week: **{transfers_used}/{TRANSFERS_PER_WEEK}**.",
            ephemeral=True, allowed_mentions=NO_PINGS
        )

    @team.command(name="remove", description="Remove a player from your team")
    async def remove(self, interaction: discord.Interaction, member: discord.Member):
        """
            Removes a player from the caller's team in the current guild.
            Build phase: immediate removal (this week).
            Post-build: queued — stops counting from next Monday 00:00 (Europe/London).

            Writes:
                TeamPlayer (closes interval or cancels a queued buy)
                TeamWeekState (refund budget)
        """
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        async with SessionLocal() as session:
            async with session.begin():
                # Team ownership
                owner = await get_or_create_user(session, interaction.user.id, discord_guild_id=guild_id)
                team = await session.scalar(select(Team).where(Team.owner_id == owner.id, Team.guild_id == guild_id))
                if not team:
                    await interaction.followup.send("Create a team first: `/team create`", ephemeral=True)
                    return

                # Ensure Player row exists
                target_user = await get_or_create_user(session, member.id, discord_guild_id=guild_id)
                player_row = await ensure_player_for_user(session, target_user)

                # Time keys
                now = datetime.now(tz=timezone.utc)
                this_week = current_week_start_norm(now)
                next_week = next_week_start_norm(now)

                # Weekly state
                state = await get_or_create_team_week_state(session, guild_id, team.id, this_week)

                # Pricing (refund amount)
                price = await get_global_player_price(session, player_row.id)
                if price is None:
                    price = 0

                this_ids_before = set(await roster_for_week(session, team.id, this_week))
                next_ids_before = set(await roster_for_week(session, team.id, next_week))

                print(f'This IDS {set(this_ids_before)}')
                print(f'Next IDS {set(next_ids_before)}')

                print(f'PlayerID {player_row.id}')

                if player_row.id not in this_ids_before and player_row.id not in next_ids_before:
                    await interaction.followup.send(f"{member.mention} is not in your team.", ephemeral=True)
                    return

                # Queue the sell (or cancel the queued buy if they only had next_week)
                await sell_player(session, team.id, player_row.id, now=now)

                # Recompute next-week roster to see if they’ll still be there after the sell
                next_ids_after = set(await roster_for_week(session, team.id, next_week))

                # Refund budget now (FPL-style)
                state.budget_remaining += price

                # Count a transfer iff they were in this week's roster and won't be in next week's roster post-sell
                if player_row.id in this_ids_before and player_row.id not in next_ids_after:
                    state.transfers_used += 1

                transfers_used = state.transfers_used

        await interaction.followup.send(
            f"Removed {member.mention}. Refunded **{price}**. "
            f"Budget remaining: **{state.budget_remaining}**. "
            f"Transfers this week: **{transfers_used}/{TRANSFERS_PER_WEEK}**.",
            ephemeral=True,
            allowed_mentions=NO_PINGS
        )

    @team.command(name="show", description="Show your current team")
    @app_commands.describe(
        week="Which roster to show: 1 = This week, 2 = Next week.",
        user="Show someone else’s team (optional)"
    )
    async def show(self, interaction: discord.Interaction, week: Optional[int] = 1,
                   user: Optional[discord.User] = None):
        """
        Shows the user's (or specified user's) fantasy team for a given gameweek, with each player's
        weekly score and a team total. Reads only.

        Week selection:
          1 => This week (current gameweek starting Monday 00:00 London)
          2 => Next week (next gameweek starting Monday 00:00 London)
        """
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this in a server (not DMs).", ephemeral=True)
            return

        # Default to "this week" if invalid week value provided
        week = 2 if week == 2 else 1

        target_user = user or interaction.user
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

        # Helpers
        def fmt_2sf(x) -> str:
            try:
                return f"{float(x):.2g}"
            except Exception:
                return "n/a"

        def fmt_1dp(x) -> str:
            try:
                return f"{float(x):.1f}"
            except Exception:
                return "n/a"

        async with SessionLocal() as session:
            async with session.begin():
                # Lookup the owner’s DB user row

                target_db_user = await get_or_create_user(session, target_user.id, discord_guild_id=guild_id)

                # Find their team in this guild
                row = await session.execute(
                    select(Team.id, Team.name)
                    .where(Team.owner_id == target_db_user.id, Team.guild_id == guild_id)
                )
                team_data = row.first()
                if not team_data:
                    await interaction.followup.send(
                        f"**{escape_mentions(target_user.display_name)}** has no team in this server.",
                        allowed_mentions=NO_PINGS
                    )
                    return

                team_id, team_name = team_data

                # Pick the gameweek to display
                now = datetime.now(tz=timezone.utc)
                this_week = current_week_start_norm(now)  # tz-aware datetime (Monday 00:00 London)
                next_week = next_week_start_norm(now)

                selected_week = next_week if week == 2 else this_week

                selected_week_key = (
                    selected_week
                    .astimezone(timezone.utc)
                    .replace(minute=0, second=0, microsecond=0, tzinfo=None)  # <-- snap 00:01 → 00:00, naive UTC
                )

                print(f'Selected week: {selected_week}')
                label = "Next Week" if week == 2 else "This Week"

                # Budget / transfers state for the selected week
                state = await get_or_create_team_week_state(session, guild_id, team_id, selected_week)

                # Roster FOR THE SELECTED WEEK:
                # Only rows whose [effective_from_week, effective_to_week) covers selected_week
                roster_rows = await session.execute(
                    select(Player.id, Player.handle, TeamPlayer.role)
                    .join(TeamPlayer, TeamPlayer.player_id == Player.id)
                    .where(
                        TeamPlayer.team_id == team_id,
                        TeamPlayer.effective_from_week <= selected_week,
                        or_(TeamPlayer.effective_to_week.is_(None), TeamPlayer.effective_to_week > selected_week),
                    )
                    .order_by(Player.handle.asc())
                )
                roster = roster_rows.all()

                if not roster:
                    await interaction.followup.send(
                        f"**{escape_mentions(target_user.display_name)}**’s team **{escape_mentions(team_name)}** "
                        f"has no players for **{label}**.",
                        allowed_mentions=NO_PINGS
                    )
                    return

                # Build player display info
                stat_rows = []
                team_total = 0.0

                for player_id, handle, role in roster:
                    user_id_num = int(handle) if str(handle).isdigit() else None
                    display_name = f"Unknown ({handle})"
                    avg_txt = score_txt = games_txt = "n/a"

                    if user_id_num:
                        # Resolve display name (best-effort)
                        display_name = await resolve_display_name(
                            interaction.guild, user_id_num, fallback=f"Unknown ({handle})"
                        )
                        display_name = escape_mentions(display_name)

                        # Fetch PlayerStats (cached averages)
                        member_row = await get_or_create_user(session, user_id_num, discord_guild_id=guild_id)
                        db_user_id = member_row.id

                        stats_row = await session.scalar(
                            select(PlayerStats)
                            .where(PlayerStats.user_id == db_user_id, PlayerStats.guild_id == guild_id)
                        )
                        if stats_row:
                            if stats_row.avg_leetify_rating is not None:
                                avg_txt = fmt_2sf(stats_row.avg_leetify_rating)
                            games_txt = str(stats_row.sample_size or 0)

                        # Weekly fantasy points for the selected gameweek
                        points_val = await session.scalar(
                            select(WeeklyPoints.weekly_score)
                            .where(
                                WeeklyPoints.user_id == db_user_id,
                                WeeklyPoints.guild_id == guild_id,
                                WeeklyPoints.week_start == selected_week_key,  #  exact week we’re showing (replaced 00:01 to 00:00)
                            )
                            .limit(1)
                        )
                        if points_val is not None:
                            score_txt = fmt_1dp(points_val)
                            try:
                                team_total += float(points_val)
                            except Exception:
                                pass

                    stat_rows.append((display_name, role or "-", avg_txt, score_txt, games_txt))

        # Build monospaced table
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

        week_str = selected_week.strftime("%Y-%m-%d %H:%M %Z")

        embed = discord.Embed(
            title=f"{escape_mentions(target_user.display_name)}’s Team — {escape_mentions(team_name)}",
            description=f"**{label}** (Gameweek starting {week_str})",
            type="rich"
        )
        embed.add_field(
            name=f"Players (Budget Remaining: {state.budget_remaining})",
            value="\n".join(lines) if stat_rows else "_(no players for this week)_",
            inline=False
        )
        embed.add_field(
            name="Team Total (this gameweek)",
            value=f"**{fmt_1dp(team_total)}**",
            inline=False
        )
        embed.set_footer(text="Players shown are those whose [from, to) interval covers this gameweek.")

        await interaction.followup.send(embed=embed, allowed_mentions=NO_PINGS)


async def setup(bot: commands.Bot):
    await bot.add_cog(Teams(bot))
