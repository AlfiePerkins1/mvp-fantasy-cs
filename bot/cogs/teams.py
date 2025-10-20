# bot/cogs/teams.py
import discord
from discord import app_commands
from discord.ext import commands


from typing import Optional
from sqlalchemy import select, func, or_, and_
from sqlalchemy.exc import IntegrityError

from discord.utils import escape_mentions
from datetime import timedelta, timezone, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.sql.functions import user

from backend.db import SessionLocal
from backend.services.market import already_on_team, roster_count, get_or_create_team_week_state, \
    get_global_player_price, TRANSFERS_PER_WEEK, buy_player, sell_player, team_has_active_this_week, roster_for_week

from backend.services.repo import get_or_create_user, create_team, ensure_player_for_user, get_user
from backend.models import Team, TeamPlayer, Player, WeeklyPoints, PlayerStats, player, User
from backend.services.leetify_api import current_week_start_london, next_week_start_london, current_week_start_norm, next_week_start_norm
from bot.cogs.stats_refresh import week_bounds_naive_utc


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

def week_start_local_naive(tz: str = "Europe/London", minute: int = 1):
    """
    Monday 00:<minute> local time, returned as a *naive* datetime.
    Use minute=1 if DB rows were written at 00:01 (legacy).
    """
    now_local = datetime.now(ZoneInfo(tz))
    start_local = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0, minute=minute, second=0, microsecond=0
    )
    return start_local.replace(tzinfo=None)



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
                user = await get_user(session, discord_id= interaction.user.id, guild_id=guild_id)

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
        Queued add (effective next Monday 00:00).
        Validate everything first (no DB mutations), then do the changes.
        """
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        async with SessionLocal() as session:
            async with session.begin():
                # fetch
                owner = await get_user(session, discord_id=interaction.user.id, guild_id=guild_id)
                team = await session.scalar(
                    select(Team).where(Team.owner_id == owner.id, Team.guild_id == guild_id)
                )
                if not team:
                    await interaction.followup.send("Create a team first: `/team create`", ephemeral=True)
                    return

                target_user = await get_user(session, discord_id=member.id, guild_id=guild_id)
                if not target_user or not target_user.steam_id:
                    await interaction.followup.send(
                        f"{member.mention} isn’t registered. Ask them to run `/account register <steamid>` first.",
                        ephemeral=True, allowed_mentions=NO_PINGS
                    )
                    return

                player_row = await ensure_player_for_user(session, target_user)

                #week keys
                now = datetime.now(tz=timezone.utc)
                # Using function from stats_refresh (It should be correct)
                this_week, next_week = week_bounds_naive_utc()

                # State used for transfer counting (this week) and budget (next week)
                state_this = await get_or_create_team_week_state(session, guild_id, team.id, this_week)
                state_next = await get_or_create_team_week_state(session, guild_id, team.id, next_week)

                #pricing
                price = await get_global_player_price(session, player_row.id)
                if price is None:
                    await interaction.followup.send(
                        "No price available for this player. Ask an admin to run `/pricing update`.",
                        ephemeral=True
                    )
                    return

                # Validate
                # roster for next week (effective_from <= next_week and (to is NULL or to > next_week))
                next_ids_before = set(await roster_for_week(session, team.id, next_week))
                if player_row.id in next_ids_before:
                    await interaction.followup.send(
                        f"{member.mention} is already queued/active for next week.",
                        ephemeral=True
                    )
                    return

                if len(next_ids_before) >= MAX_TEAM_SIZE:
                    await interaction.followup.send(
                        f"Your team is full for next week (max {MAX_TEAM_SIZE}).",
                        ephemeral=True
                    )
                    return

                if (state_next.budget_remaining or 0) < price:
                    await interaction.followup.send(
                        f"Not enough budget. Price: {price}, remaining: {state_next.budget_remaining}.",
                        ephemeral=True
                    )
                    return

                post_first_lock = await team_has_active_this_week(session, team.id, this_week)
                # Only after the first lock can we enforce transfer cap
                if post_first_lock and TRANSFERS_PER_WEEK and (state_this.transfers_used or 0) >= TRANSFERS_PER_WEEK:
                    await interaction.followup.send(
                        f"You’ve already used your {TRANSFERS_PER_WEEK} transfer(s) this week.",
                        ephemeral=True
                    )
                    return

                # If passed all validations then mutate
                session.add(TeamPlayer(
                    team_id=team.id,
                    player_id=player_row.id,
                    role=role,
                    effective_from_week=next_week,
                    effective_to_week=None
                ))

                # Budget applies to next-week’s state (FPL-style)
                state_next.budget_remaining = (state_next.budget_remaining or 0) - price

                # Count a transfer (this week) only if we weren’t already active this week
                transfers_used = state_this.transfers_used or 0
                if post_first_lock:
                    this_ids = set(await roster_for_week(session, team.id, this_week))
                    if player_row.id not in this_ids:
                        state_this.transfers_used = transfers_used + 1
                        transfers_used = state_this.transfers_used

        # If we got to this bit then it was all good
        await interaction.followup.send(
            f"Added {member.mention}{f' as **{role}**' if role else ''}. "
            f"Price: **{price}**. Budget remaining: **{state_next.budget_remaining}**. "
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
                owner = await get_user(session, discord_id= interaction.user.id, guild_id=guild_id)
                team = await session.scalar(select(Team).where(Team.owner_id == owner.id, Team.guild_id == guild_id))
                if not team:
                    await interaction.followup.send("Create a team first: `/team create`", ephemeral=True)
                    return

                # Ensure Player row exists
                target_user = await get_user(session, discord_id= member.id, guild_id=guild_id)
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
        user="Show someone else’s team (optional)",
    )
    async def show(self, interaction: discord.Interaction, week: Optional[int] = 1,
                   user: Optional[discord.User] = None):
        guild = interaction.guild
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this in a server (not DMs).", ephemeral=True)
            return

        week = 2 if week == 2 else 1
        target_user = user or interaction.user
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

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

        # Week keys (London local-naive)
        base_start, _ = week_bounds_naive_utc("Europe/London")
        selected_start = base_start if week == 1 else (base_start + timedelta(days=7))
        selected_end = selected_start + timedelta(days=7)
        label = "This Week" if week == 1 else "Next Week"

        # Tolerance


        async with SessionLocal() as session:
            async with session.begin():
                # Find the caller's DB user (no create here)
                target_db_user = await session.scalar(
                    select(User).where(
                        User.discord_id == int(target_user.id),
                        User.discord_guild_id == guild_id
                    )
                )
                if not target_db_user:
                    await interaction.followup.send(
                        f"**{escape_mentions(target_user.display_name)}** has no account in this server.",
                        allowed_mentions=NO_PINGS
                    )
                    return

                # Team for this guild
                team_row = await session.execute(
                    select(Team.id, Team.name)
                    .where(Team.owner_id == target_db_user.id, Team.guild_id == guild_id)
                )
                row = team_row.first()
                if not row:
                    await interaction.followup.send(
                        f"**{escape_mentions(target_user.display_name)}** has no team in this server.",
                        allowed_mentions=NO_PINGS
                    )
                    return
                team_id, team_name = row
                state = await get_or_create_team_week_state(session, guild_id, team_id, selected_start)
                bud_rem = float(state.budget_remaining)

                # Roster active in the selected week: interval *overlap*
                roster_rows = await session.execute(
                    select(Player.id, Player.handle, TeamPlayer.role)
                    .join(TeamPlayer, TeamPlayer.player_id == Player.id)
                    .where(
                        TeamPlayer.team_id == team_id,
                        TeamPlayer.effective_from_week < selected_end,
                        or_(
                            TeamPlayer.effective_to_week.is_(None),
                            TeamPlayer.effective_to_week > selected_start,
                        ),
                    )
                    .order_by(Player.handle.asc())
                )
                roster = roster_rows.all()

                if not roster:
                    await interaction.followup.send(
                        f"**{escape_mentions(target_user.display_name)}**’s team **{escape_mentions(team_name)}** "
                        f"has no players for **{label}**.\n"
                        f"**Budget remaining:** ${bud_rem:,.0f}",
                        allowed_mentions=NO_PINGS
                    )
                    return


                # Map handles
                handle_ids = [int(h) for _, h, _ in roster if str(h).isdigit()]
                users_in_guild = await session.execute(
                    select(User.discord_id, User.id)
                    .where(User.discord_id.in_(handle_ids), User.discord_guild_id == guild_id)
                )
                discord_to_userid = dict(users_in_guild.tuples().all())
                db_user_ids = [discord_to_userid.get(int(h)) for _, h, _ in roster if str(h).isdigit()]
                db_user_ids = [uid for uid in db_user_ids if uid is not None]

                # Weekly points for the selected week/guild
                latest_per_user = (
                    select(
                        WeeklyPoints.user_id,
                        func.max(WeeklyPoints.computed_at).label("latest_ts")
                    )
                    .where(
                        WeeklyPoints.guild_id == guild_id,
                        WeeklyPoints.user_id.in_(db_user_ids),
                        WeeklyPoints.weekly_score.isnot(None),
                        WeeklyPoints.computed_at.isnot(None),
                        WeeklyPoints.ruleset_id == 1, # Rulset filter for future
                    )
                    .group_by(WeeklyPoints.user_id)
                ).subquery()

                wp_rows = await session.execute(
                    select(WeeklyPoints.user_id, WeeklyPoints.weekly_score)
                    .join(
                        latest_per_user,
                        and_(
                            WeeklyPoints.user_id == latest_per_user.c.user_id,
                            WeeklyPoints.computed_at == latest_per_user.c.latest_ts,
                        )
                    )
                )
                points_map: dict[int, float] = dict(wp_rows.tuples().all())

                missing_uids = [uid for uid in db_user_ids if uid not in points_map]
                if missing_uids:
                    # map missing user_id -> discord_id
                    uid_to_discord = dict(
                        (u_id, d_id)
                        for d_id, u_id in discord_to_userid.items()
                    )
                    missing_dids = [uid_to_discord.get(uid) for uid in missing_uids if uid_to_discord.get(uid)]
                    if missing_dids:
                        # all user_ids across ANY guild that share these discord_ids
                        any_user_ids = await session.execute(
                            select(User.id).where(User.discord_id.in_(missing_dids))
                        )
                        any_user_ids = [r[0] for r in any_user_ids.all()]
                        if any_user_ids:
                            latest_any = (
                                select(
                                    WeeklyPoints.user_id,
                                    func.max(WeeklyPoints.computed_at).label("latest_ts")
                                )
                                .where(
                                    WeeklyPoints.user_id.in_(any_user_ids),
                                    WeeklyPoints.weekly_score.isnot(None),
                                    WeeklyPoints.computed_at.isnot(None),
                                    WeeklyPoints.ruleset_id == 1,
                                )
                                .group_by(WeeklyPoints.user_id)
                            ).subquery()

                            wp_any = await session.execute(
                                select(WeeklyPoints.user_id, WeeklyPoints.weekly_score)
                                .join(
                                    latest_any,
                                    and_(
                                        WeeklyPoints.user_id == latest_any.c.user_id,
                                        WeeklyPoints.computed_at == latest_any.c.latest_ts,
                                    )
                                )
                            )
                            rows_any = wp_any.tuples().all()

                            # map those "any guild" user_ids back to this guild's uid via discord_id
                            id_to_did = dict(
                                (u_id, d_id)
                                for u_id, d_id in (await session.execute(
                                    select(User.id, User.discord_id).where(User.id.in_([u for (u, _) in rows_any]))
                                )).tuples().all()
                            )
                            for uid_any, score in rows_any:
                                did = id_to_did.get(uid_any)
                                uid_here = discord_to_userid.get(did)
                                if uid_here is not None and uid_here not in points_map:
                                    points_map[uid_here] = score

                # Player averages for display
                ps_rows = await session.execute(
                    select(PlayerStats.user_id, PlayerStats.avg_leetify_rating, PlayerStats.sample_size)
                    .where(PlayerStats.guild_id == guild_id, PlayerStats.user_id.in_(db_user_ids))
                )
                stats_map = {uid: (avg, n) for uid, avg, n in ps_rows.all()}

                # Budget/state for the same key
                state = await get_or_create_team_week_state(session, guild_id, team_id, selected_start)

                # Build table + total
                stat_rows = []
                team_total = 0.0
                for player_id, handle, role in roster:
                    disp = f"Unknown ({handle})"
                    avg_txt, score_txt, games_txt = "n/a", "n/a", "0"

                    if str(handle).isdigit():
                        did = int(handle)
                        disp_name = await resolve_display_name(guild, did, fallback=f"Unknown ({handle})")
                        disp = escape_mentions(disp_name)
                        uid = discord_to_userid.get(did)
                        if uid:
                            avg, n = stats_map.get(uid, (None, 0))
                            if avg is not None:
                                avg_txt = fmt_2sf(avg)
                            games_txt = str(n or 0)
                            pts = points_map.get(uid)
                            if pts is not None:
                                score_txt = fmt_1dp(pts)
                                try:
                                    team_total += float(pts)
                                except:
                                    pass

                    stat_rows.append((disp, role or "-", avg_txt, score_txt, games_txt))

        # Render table
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
        for r in stat_rows: lines.append(fmt_row(r, widths))
        lines.append("```")

        embed = discord.Embed(
            title=f"{escape_mentions(target_user.display_name)}’s Team — {escape_mentions(team_name)}",
            description=f"**{label}** (Gameweek starting {selected_start})",
            type="rich",
        )
        embed.add_field(
            name=f"Players (Budget Remaining: {state.budget_remaining})",
            value="\n".join(lines) if stat_rows else "_(no players for this week)_",
            inline=False,
        )
        embed.add_field(name="Team Total (this gameweek)", value=f"**{fmt_1dp(team_total)}**", inline=False)
        embed.set_footer(text="Players shown are those whose [from, to) interval overlaps this gameweek.")
        await interaction.followup.send(embed=embed, allowed_mentions=NO_PINGS)

    @team.command(name="change_name", description="Change the name of your team")
    @app_commands.describe(new_name="The name of the team")
    async def team_change_name(self, interaction: discord.Interaction, new_name: str):
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return

        # normalize input and set max length
        MAX_TEAM_NAME = 64
        name = " ".join(new_name.strip().split())  # trim + collapse spaces
        if not name:
            await interaction.response.send_message("Team name cannot be empty.", ephemeral=True)
            return
        if len(name) > MAX_TEAM_NAME:
            await interaction.response.send_message(f"Team name must be ≤ {MAX_TEAM_NAME} characters.", ephemeral=True)
            return

        async with SessionLocal() as session:
            async with session.begin():
                # fetch the caller's team in this guild
                user = await get_user(session, discord_id= interaction.user.id, guild_id=guild_id)
                team = await session.scalar(
                    select(Team).where(Team.owner_id == user.id, Team.guild_id == guild_id)
                )
                if team is None:
                    await interaction.response.send_message("You don't have a team in this server.", ephemeral=True)
                    return

                # dont change if unchanged
                if team.name == name:
                    await interaction.response.send_message(f"Your team is already named **{name}**.", ephemeral=True)
                    return

                # pre-check to avoid IntegrityError message (it should never occur but idk)
                taken = await session.scalar(
                    select(func.count())
                    .select_from(Team)
                    .where(
                        Team.guild_id == guild_id,
                        func.lower(Team.name) == name.lower(),
                        Team.id != team.id,
                    )
                )
                if taken:
                    await interaction.response.send_message("That team name is already taken in this server.",
                                                            ephemeral=True)
                    return

                # update
                team.name = name
                try:
                    # session.begin() will commit
                    pass
                except IntegrityError:
                    await session.rollback()
                    await interaction.response.send_message("That team name is already taken in this server.",
                                                            ephemeral=True)
                    return

        await interaction.response.send_message(f"Team renamed to **{name}**.")

    @team.command(name='transfers', description="View upcoming transfers")
    async def view_transfers(self, interaction: discord.Interaction):

        guild = interaction.guild
        guild_id = interaction.guild_id

        await interaction.response.defer(ephemeral=False, thinking=True)

        if not guild_id:
            await interaction.response.send_message("Use this in a server (not DMs).", ephemeral=True)
            return
        target_user = interaction.user

        current_date = now = datetime.now(timezone.utc)
        base_start, _ = week_bounds_naive_utc("Europe/London")
        selected_start = base_start
        selected_end = selected_start + timedelta(days=7)

        async with SessionLocal() as session:
            async with session.begin():

                target_db_user = await session.scalar(
                    select(User).where(
                        User.discord_id == int(target_user.id),
                        User.discord_guild_id == guild_id,
                    )
                )
                if not target_db_user:
                    await interaction.followup.send(
                        f'**{escape_mentions(target_user.display_name)}** has no account in this server.',
                        allowed_mentions=NO_PINGS
                    )
                    return

                team_row = await session.execute(
                    select(Team.id, Team.name)
                    .where(Team.owner_id == target_db_user.id, Team.guild_id == guild_id)
                )
                row = team_row.first()
                if not row:
                    await interaction.followup.send(
                        f"**{escape_mentions(target_user.display_name)}** has no team in this server.",
                        allowed_mentions=NO_PINGS
                    )
                    return

                team_id, team_name = row
                state = await get_or_create_team_week_state(session, guild_id, team_id, selected_start)
                bud_rem = float(state.budget_remaining)

                transfer_rows_in = await session.execute(
                    select(
                        Player.id,
                        Player.handle,
                        TeamPlayer.role,
                        TeamPlayer.effective_from_week.label("start_at"),
                        Player.price,
                    )
                    .join(TeamPlayer, TeamPlayer.player_id == Player.id)
                    .where(
                        TeamPlayer.team_id == team_id,
                        TeamPlayer.effective_from_week > current_date,
                    ).order_by(Player.handle.asc())
                )
                transfers_in = transfer_rows_in.all()

                transfer_rows_out = await session.execute(
                    select(
                        Player.id,
                        Player.handle,
                        TeamPlayer.role,
                        TeamPlayer.effective_to_week.label("leave_at"),
                        Player.price,
                    )
                    .join(TeamPlayer, TeamPlayer.player_id == Player.id)
                    .where(
                        TeamPlayer.team_id == team_id,
                        TeamPlayer.effective_to_week > current_date,
                    ).order_by(Player.handle.asc())
                )
                transfers_out = transfer_rows_out.all()

        def _name_from_handle(h: str) -> str:
            return f"<@{h}>" if str(h).isdigit() else f"{h}"

        def _fmt_date(d) -> str:
            try:
                return d.strftime("%d-%b")
            except Exception:
                return "TBD"

        def _fmt_money(v) -> str:
            try:
                return f"{int(v):,}"
            except Exception:
                return "-"

        lines_in = []
        for _pid, handle, _role, start_at, price in transfers_in:
            lines_in.append(f"{_name_from_handle(handle)} | {_fmt_date(start_at)} | - {_fmt_money(price)}")

        lines_out = []
        for _pid, handle, _role, leave_at, price in transfers_out:
            lines_out.append(f"{_name_from_handle(handle)} | {_fmt_date(leave_at)} | + {_fmt_money(price)}")

        # Fallbacks if empty
        block_in = "\n".join(lines_in) if lines_in else "None"
        block_out = "\n".join(lines_out) if lines_out else "    None    "

        budget_txt = f"${int(bud_rem):,}"

        embed = discord.Embed(
            title=f"Transfers — {team_name}",
            description="Queued changes for the upcoming gameweek",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Transfers In", value="User    | Start Date | - Cost\n" + block_in, inline=True)
        embed.add_field(name="Transfers Out", value="User | Leaving Date | + Cost\n" + block_out, inline=True)
        embed.add_field(name="Overall Budget Remaining", value=f"**{budget_txt}**", inline=False)

        await interaction.followup.send(embed=embed, allowed_mentions=NO_PINGS)



async def setup(bot: commands.Bot):
    await bot.add_cog(Teams(bot))
