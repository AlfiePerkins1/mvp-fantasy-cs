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

from backend.services.scoring import compute_weekly_from_playerstats

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

        # Defer so we don't trip Discord's 3s limit
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)

        # small formatters
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

        # scoring from a freshly aggregated dict (same math as services.scoring)
        MATCH_MULT = {"premier": 1.20, "faceit": 1.10, "renown": 1.00, "mm": 0.80}

        def compute_weekly_from_agg(agg: dict, alpha: float = 10.0, k: float = 0.60, cap: float = 1.15):
            def v(key, d=0.0):
                val = agg.get(key)
                return float(val) if val is not None else float(d)

            # games
            g_counts = int(v("premier_games", 0)) + int(v("faceit_games", 0)) + int(v("renown_games", 0)) + int(
                v("mm_games", 0))
            games_total = int(agg.get("sample_size") or g_counts or 0)
            if games_total <= 0:
                return {"base_avg": 0.0, "avg_mult": 1.0, "weekly_base": 0.0, "weekly_score": 0.0}

            base_avg = (
                    20.0 * v("avg_leetify_rating") +
                    0.1 * v("adr") +
                    2.0 * (v("trade_kills") / games_total) +
                    3.0 * (v("entries") / games_total) +
                    1.0 * (v("flashes") / games_total) +
                    0.05 * (v("util_dmg") / games_total)
            )

            n_prem = int(v("premier_games", 0))
            n_face = int(v("faceit_games", 0))
            n_ren = int(v("renown_games", 0))
            n_mm = int(v("mm_games", 0))
            denom = max(1, n_prem + n_face + n_ren + n_mm)
            avg_mult = (
                               MATCH_MULT["premier"] * n_prem +
                               MATCH_MULT["faceit"] * n_face +
                               MATCH_MULT["renown"] * n_ren +
                               MATCH_MULT["mm"] * n_mm
                       ) / denom

            weekly_base = base_avg * avg_mult

            wins = int(v("wins", 0))
            wr_eff = (wins + alpha * 0.5) / (games_total + alpha)
            wr_mult = min(1.0 + max(0.0, wr_eff - 0.5) * k, cap)

            weekly_score = weekly_base * wr_mult
            return {"base_avg": base_avg, "avg_mult": avg_mult, "weekly_base": weekly_base,
                    "weekly_score": weekly_score}

        # DB READ ONLY
        team_name = None
        team_id = None
        roster: list[tuple[str, Optional[str]]] = []
        players_plan = []  # dicts with keys: handle, role, user_id, db_user_id, steam_id, cached, needs_refresh

        async with SessionLocal() as session:
            async with session.begin():
                target_db_user = await get_or_create_user(session, target_user.id)

                team_row = (await session.execute(
                    select(Team.id, Team.name).where(
                        Team.owner_id == target_db_user.id,
                        Team.guild_id == guild_id
                    )
                )).first()
                if not team_row:
                    await interaction.followup.send(
                        f"**{escape_mentions(target_user.display_name)}** has no team in this server.",
                        allowed_mentions=NO_PINGS
                    )
                    return

                team_id, team_name = team_row

                roster = (await session.execute(
                    select(Player.handle, TeamPlayer.role)
                    .join(TeamPlayer, TeamPlayer.player_id == Player.id)
                    .where(TeamPlayer.team_id == team_id)
                    .order_by(Player.handle.asc())
                )).all()

                for handle, role in roster:
                    text = str(handle)
                    user_id_num = int(text) if text.isdigit() else None

                    db_user_id = None
                    steam_id = None
                    cached = None
                    refresh = False

                    if user_id_num:
                        member_row = await get_or_create_user(session, user_id_num)
                        db_user_id = member_row.id
                        steam_id = member_row.steam_id
                        cached = await get_cached_stats(session, db_user_id, guild_id)
                        refresh = is_stale(cached) and bool(steam_id)

                    players_plan.append({
                        "handle": text,
                        "role": role,
                        "user_id": user_id_num,
                        "db_user_id": db_user_id,
                        "steam_id": steam_id,
                        "cached": cached,
                        "needs_refresh": refresh,
                    })

        #  NETWORK (outside DB)
        aggregates: dict[int, dict] = {}
        for plan in players_plan:
            if not plan["needs_refresh"]:
                continue
            try:
                matches = await fetch_recent_matches(plan["steam_id"], limit=20)
                agg = aggregate_player_stats(matches, plan["steam_id"], week_start_london=week_start)
                aggregates[plan["db_user_id"]] = agg
            except Exception as e:
                print(f"[show] aggregate failed for user_id={plan['user_id']}: {e}")

        # DB WRITE (UPSERT)
        if aggregates:
            async with SessionLocal() as session:
                async with session.begin():
                    for db_user_id, agg in aggregates.items():
                        try:
                            await upsert_stats(
                                session,
                                user_id=db_user_id,
                                guild_id=guild_id,
                                avg_leetify_rating=agg.get("avg_leetify_rating"),
                                sample_size=agg.get("sample_size"),
                                trade_kills=agg.get("trade_kills"),
                                ct_rating=agg.get("ct_rating"),
                                t_rating=agg.get("t_rating"),
                                adr=agg.get("adr"),
                                entries=agg.get("entries"),
                                flashes=agg.get("flashes"),
                                util_dmg=agg.get("util_dmg"),
                                faceit_games=agg.get("faceit_games"),
                                premier_games=agg.get("premier_games"),
                                renown_games=agg.get("renown_games"),
                                mm_games=agg.get("mm_games"),
                                other_games=agg.get("other_games"),
                                wins=agg.get("wins"),
                            )
                        except Exception as e:
                            print(f"[show] upsert failed for db_user_id={db_user_id}: {e}")

        # BUILD TABLE
        stat_rows = []  # (display, role, avg_txt, score_txt, games_txt)

        for plan in players_plan:
            text = plan["handle"]
            role = plan["role"] or "-"

            # Resolve display name
            if plan["user_id"]:
                display = await resolve_display_name(interaction.guild, plan["user_id"], fallback=f"Unknown ({text})")
            else:
                display = text
            display = escape_mentions(display)

            avg_txt = "n/a"
            score_txt = "n/a"
            games_txt = "n/a"

            # Source for numbers: prefer fresh aggregate, else cached row
            src = aggregates.get(plan["db_user_id"]) if plan["db_user_id"] in aggregates else plan["cached"]

            if src:
                if isinstance(src, dict):
                    # fresh aggregate dict
                    avg_val = src.get("avg_leetify_rating")
                    if avg_val is not None:
                        avg_txt = fmt_2sf(avg_val)
                    games_txt = f"{int(src.get('sample_size') or 0)}"
                    out = compute_weekly_from_agg(src)
                    score_txt = fmt_1dp(out["weekly_score"])
                else:
                    # cached ORM row
                    if src.avg_leetify_rating is not None:
                        avg_txt = fmt_2sf(src.avg_leetify_rating)
                    games_txt = f"{int(src.sample_size or 0)}"
                    out = compute_weekly_from_playerstats(src)
                    score_txt = fmt_1dp(out["weekly_score"])

            stat_rows.append((display, role, avg_txt, score_txt, games_txt))

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
        embed.set_footer(text=f"Leetify averages since {start_str} (London Time)")

        await interaction.followup.send(embed=embed, allowed_mentions=NO_PINGS)

async def setup(bot: commands.Bot):
    await bot.add_cog(Teams(bot))
