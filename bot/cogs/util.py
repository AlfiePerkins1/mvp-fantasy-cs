# bot/cogs/util.py
import discord
from discord import app_commands
from discord.ext import commands
from requests.sessions import Session

from sqlalchemy.ext.asyncio import AsyncEngine

from sqlalchemy import text, or_, update, delete, select, update, cast, BigInteger
from backend.db import engine as async_engine
from backend.db import SessionLocal
from backend.models import TeamPlayer, Team, TeamWeekState, User, PlayerGame
from backend.services.faceit_api import fetch_faceit_guid_by_steam, fetch_faceit_match_elo_for_player, \
    fetch_faceit_team_avg_elo
from backend.services.market import get_or_create_team_week_state
from bot.cogs.stats_refresh import week_bounds_naive_utc

from sqlalchemy.ext.asyncio import AsyncSession

SYSTEM_AD_ID = [276641144128012289]
INITIAL_BUDGET = 30000

def system_admin_only():
    def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id not in SYSTEM_AD_ID:
            # returning False also works, but raising gives a clearer error you can catch
            raise app_commands.CheckFailure("This command is restricted.")
        return True
    return app_commands.check(predicate)


async def set_faceit_id_for_steam(
    session: AsyncSession,
    steam_id: int | str,
    faceit_guid: str | None,
) -> int:
    """
    Writes `faceit_id` for *all* User rows that have this steam_id.
    Returns number of rows updated.
    """
    if not faceit_guid:
        return 0

    stmt = (
        update(User)
        .where(User.steam_id == int(steam_id))
        .values(faceit_id=faceit_guid)
    )
    res = await session.execute(stmt)
    return res.rowcount or 0


async def fill_missing_faceit_elo(session: AsyncSession) -> tuple[int, int,int]:

    rows = (await session.execute(
        select(
            PlayerGame.id,
            PlayerGame.match_game_id,
            PlayerGame.steam_id,
            User.faceit_id,
        )
        .join(
            User,
            User.steam_id == cast(PlayerGame.steam_id, BigInteger),
            isouter=True,
        )
        .where(
            PlayerGame.match_game_id.is_not(None),
            PlayerGame.faceit_player_elo.is_(None),
            PlayerGame.data_source == "faceit",
        )
    )).all()

    print(f"[elo-fill] candidate rows: {len(rows)}")

    if not rows:
        return(0,0,0)

    by_guid = {}

    skip_no_guid = 0

    for pg_id, match_id, steam_id, faceit_guid in rows:
        if not faceit_guid:
            skip_no_guid += 1
            continue
        by_guid.setdefault(faceit_guid, []).append((pg_id, match_id, steam_id))

    updated, not_found = 0,0

    for guid, items in by_guid.items():

        print(f"[elo-fill] querying Faceit stats for guid={guid} (items={len(items)})")

        docs = await fetch_faceit_match_elo_for_player(guid)
        elo_by_match = {}
        for d in docs or []:
            mid = d.get("matchId")
            elo = d.get("elo")
            if mid and elo is not None:
                elo_by_match[str(mid).strip()] = int(elo)

        for pg_id, match_id, _steam in items:
            key = str(match_id or "").strip()
            elo = elo_by_match.get(key)
            if elo is None:
                not_found += 1
                continue
            await session.execute(
                update(PlayerGame)
                .where(PlayerGame.id == pg_id)
                .values(faceit_player_elo=int(elo))
            )
            updated += 1

    await session.flush()
    return (updated, skip_no_guid, not_found)


async def fill_faceit_avg_elo(session) -> tuple[int, int]:
    """
    Finds Faceit matches with a match_game_id and missing faceit_avg_elo,
    fetches team averages from the v4 match endpoint, and updates all rows for that match.
    Returns (updated_matches, skipped_matches).
    """
    # unique match ids to avoid fetching 10x
    match_ids = (await session.execute(
        select(PlayerGame.match_game_id)
        .where(
            PlayerGame.data_source == "faceit",
            PlayerGame.match_game_id.isnot(None),
            PlayerGame.faceit_avg_elo.is_(None),
        )
        .group_by(PlayerGame.match_game_id)
    )).scalars().all()

    updated = 0
    skipped = 0

    for mid in match_ids:
        try:
            t1, t2, lobby = await fetch_faceit_team_avg_elo(mid)
            if lobby is None:
                skipped += 1
                continue

            # Store only the lobby average:
            await session.execute(
                update(PlayerGame)
                .where(PlayerGame.match_game_id == mid)
                .values(faceit_avg_elo=int(lobby))
            )

            updated += 1
        except Exception as e:
            # log and continue
            print(f"[faceit-backfill] match {mid} failed: {e}")
            skipped += 1

    await session.flush()
    return updated, skipped

def _is_admin_only(cmd: app_commands.Command | app_commands.Group) -> bool:
    dp = getattr(cmd, "default_permissions", None)
    return bool(dp and getattr(dp, "administrator", False))

def _flatten(commands_list, parents=None):
    """Yield tuples of (path_parts[list[str]], description, admin_only, obj)."""
    parents = parents or []
    for c in commands_list:
        if isinstance(c, app_commands.Group):
            # yield the group itself
            yield (parents + [c.name], c.description or "", _is_admin_only(c), c)
            # recurse into children
            yield from _flatten(list(c.commands), parents + [c.name])
        else:
            yield (parents + [c.name], c.description or "", _is_admin_only(c), c)

class Util(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="commands", description="Show all available slash commands for this bot, grouped.")
    async def list_commands(self, interaction: discord.Interaction):
        """
            Just lists the avaliable commands for the server

        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        all_cmds = list(self.bot.tree.get_commands())
        flat = list(_flatten(all_cmds))
        if not flat:
            await interaction.followup.send("No slash commands registered.", ephemeral=True)
            return

        # Group by top-level segment
        grouped: dict[str, list[tuple[str, str, bool]]] = {}
        for parts, desc, is_admin, obj in flat:
            top = parts[0] if parts else "misc"
            path_str = "/" + " ".join(parts)
            grouped.setdefault(top, []).append((path_str, desc, is_admin))

        # Desired display order; any others will follow alphabetically
        preferred = ["account", "leaderboard", "player", "pricing", "scoring", "team", "misc"]
        keys = [k for k in preferred if k in grouped] + sorted([k for k in grouped if k not in preferred])

        # Build embed
        embed = discord.Embed(
            title="Bot Commands",
            description="Grouped by top-level command. Admin-only commands are marked.",
            color=discord.Color.blurple()
        )

        for k in keys:
            nice = k.capitalize()
            # sort so the group root (e.g. "/account") is shown first
            items = sorted(grouped[k], key=lambda t: (t[0].count(" "), t[0]))
            # Build block text for this category; split into multiple fields if >1024 chars
            lines, fields = [], []
            for path, desc, is_admin in items:
                suffix = " **(admin only)**" if is_admin else ""
                # show the root ("/account") without a dash if it’s a group line
                if path.count(" ") == 0:
                    lines.append(f"`{path}` — {desc or 'No description'}{suffix}")
                else:
                    lines.append(f"• `{path}` — {desc or 'No description'}{suffix}")

            # chunk to respect 1024 char field limit
            buf = ""
            for line in lines:
                if len(buf) + len(line) + 1 > 1024:
                    fields.append(buf)
                    buf = line
                else:
                    buf += ("\n" if buf else "") + line
            if buf:
                fields.append(buf)
            for i, chunk in enumerate(fields, start=1):
                name = nice if i == 1 else f"{nice} (cont.)"
                embed.add_field(name=name, value=chunk, inline=False)

        embed.set_footer(text="Admin-only detected via default_permissions(administrator=True).")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="fix_users_schema", description="SQLite: rebuild users for per-guild uniqueness")
    @system_admin_only()
    async def fix_users_schema(self, interaction: discord.Interaction):
        await interaction.response.send_message("Rebuilding users table…", ephemeral=True)

        engine: AsyncEngine = async_engine

        async with engine.begin() as conn:
            # turn off FKs temporarily
            await conn.exec_driver_sql("PRAGMA foreign_keys=OFF;")

            # create new table with correct schema
            await conn.exec_driver_sql("""
                                       CREATE TABLE IF NOT EXISTS users_new
                                       (
                                           id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                                           discord_id           BIGINT NOT NULL,
                                           discord_guild_id     BIGINT NOT NULL,
                                           steam_id             TEXT,
                                           faceit_id            TEXT,
                                           discord_username     TEXT,
                                           discord_global_name  TEXT,
                                           discord_display_name TEXT,
                                           has_leetify          BOOLEAN
                                       );
                                       """)

            #migrate existing data; put NULL guilds to ligans disc
            await conn.exec_driver_sql("""
                                       INSERT INTO users_new (id, discord_id, discord_guild_id, steam_id, faceit_id,
                                                              discord_username, discord_global_name,
                                                              discord_display_name, has_leetify)
                                       SELECT id,
                                              discord_id,
                                              COALESCE(discord_guild_id, 1366544715012640798),
                                              steam_id,
                                              faceit_id,
                                              discord_username,
                                              discord_global_name,
                                              discord_display_name,
                                              has_leetify
                                       FROM users;
                                       """)

            # swap tables
            await conn.exec_driver_sql("DROP TABLE users;")
            await conn.exec_driver_sql("ALTER TABLE users_new RENAME TO users;")

            # indexes/constraints
            await conn.exec_driver_sql("""
                                       CREATE UNIQUE INDEX IF NOT EXISTS uq_user_per_guild
                                           ON users (discord_id, discord_guild_id);
                                       """)
            await conn.exec_driver_sql("""
                                       CREATE INDEX IF NOT EXISTS ix_users_discord_guild
                                           ON users (discord_id, discord_guild_id);
                                       """)

            await conn.exec_driver_sql("PRAGMA foreign_keys=ON;")

        await interaction.followup.send("Users table rebuilt with unique(discord_id, discord_guild_id).",
                                        ephemeral=True)

    @app_commands.command(
        name="reset_teams",
        description="Reset all teams to be empty for new season start"
    )
    @system_admin_only()
    @app_commands.describe(all_guilds="If true, reset EVERY server; otherwise only this server.")
    async def reset_teams(self, interaction: discord.Interaction, all_guilds: bool = False):
        await interaction.response.defer(ephemeral=True, thinking=True)

        week_start, week_end = week_bounds_naive_utc("Europe/London")  # [start, end) of THIS week
        guild_id = interaction.guild_id

        # Build a team-id subquery based on scope (i.e. all servers or just 1)
        if all_guilds:
            team_ids_subq = select(Team.id).subquery()
            team_filter_for_update = True  # no-op; we'll omit WHERE
        else:
            team_ids_subq = select(Team.id).where(Team.guild_id == guild_id).subquery()
            team_filter_for_update = (Team.guild_id == guild_id)

        total_closed = 0
        total_deleted_future = 0
        teams_reset = 0

        async with SessionLocal() as session:
            async with session.begin():

                # Close ongoing TeamPlayer rows at the end of this week
                close_stmt = (
                    update(TeamPlayer)
                    .where(
                        TeamPlayer.team_id.in_(select(team_ids_subq)),
                        TeamPlayer.effective_from_week < week_end,
                        or_(
                            TeamPlayer.effective_to_week.is_(None),
                            TeamPlayer.effective_to_week > week_end,
                        ),
                    )
                    .values(effective_to_week=week_end)
                )
                res_close = await session.execute(close_stmt)
                total_closed = res_close.rowcount or 0

                # Delete future-dated team rows (queued adds for >= next week)
                delete_stmt = (
                    delete(TeamPlayer)
                    .where(
                        TeamPlayer.team_id.in_(select(team_ids_subq)),
                        TeamPlayer.effective_from_week >= week_end,
                    )
                )
                res_del = await session.execute(delete_stmt)
                total_deleted_future = res_del.rowcount or 0

                # Reset Team.build_complete
                if all_guilds:
                    await session.execute(update(Team).values(build_complete=False))
                else:
                    await session.execute(
                        update(Team).where(team_filter_for_update).values(build_complete=False)
                    )

                # Set TeamWeekState with fresh budget/transfers
                teams_stmt = select(Team.id, Team.guild_id)
                if not all_guilds:
                    teams_stmt = teams_stmt.where(Team.guild_id == guild_id)

                teams = (await session.execute(teams_stmt)).all()

                for team_id, g_id in teams:
                    state = await get_or_create_team_week_state(
                        session, guild_id=g_id, team_id=team_id, week_start=week_end
                    )
                    state.budget_remaining = INITIAL_BUDGET
                    state.transfers_used = 0
                    teams_reset += 1

        scope = "ALL SERVERS" if all_guilds else "this server"
        await interaction.followup.send(
            f"Season reset for **{scope}**\n"
            f"- Closed active TeamPlayer rows at GW end: **{total_closed}**\n"
            f"- Deleted future teams rows: **{total_deleted_future}**\n"
            f"- Set budget to (${INITIAL_BUDGET:.0f}) & reset transfers for **{teams_reset}** team(s).",
            ephemeral=True,
        )

    @app_commands.command(name="update_faceit_guid", description="Update user table with faceit guid's")
    @app_commands.describe(all_guilds="If true, process users from EVERY guild")
    @system_admin_only()
    async def update_faceit_guid(self, interaction: discord.Interaction, all_guilds: bool = False):
        guild_id = interaction.guild_id
        if not guild_id and not all_guilds:
            await interaction.response.send_message("Use this in a server (or pass all_guilds=True).", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        updated, missing, errors = 0, 0, 0

        async with SessionLocal() as session:
            # pick users with steam_id and missing faceit_id
            q = select(User.steam_id).where(User.steam_id.is_not(None), User.faceit_id.is_(None))
            if not all_guilds:
                q = q.where(User.discord_guild_id == guild_id)

            steam_ids = [int(s) for (s,) in (await session.execute(q)).all()]
            # de-dupe per steam_id to avoid double lookups across guild copies
            for steam in sorted(set(steam_ids)):
                try:
                    guid = await fetch_faceit_guid_by_steam(steam)
                    if guid:
                        n = await set_faceit_id_for_steam(session, steam_id=steam, faceit_guid=guid)
                        updated += n
                    else:
                        missing += 1
                except Exception as e:
                    errors += 1
                    # optional: log e
            await session.commit()

        scope = "all guilds" if all_guilds else "this server"
        await interaction.followup.send(
            f"Faceit GUID linking complete for {scope}.\n"
            f"Updated rows: **{updated}** • Not found: **{missing}** • Errors: **{errors}**",
            ephemeral=True,
        )

    @app_commands.command(
        name="fill_faceit_elo",
        description="Backfill Faceit per-match ELO into player_games where missing"
    )
    @system_admin_only()
    async def fill_faceit_elo_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with SessionLocal() as session:
            async with session.begin():
                updated, skipped, not_found = await fill_missing_faceit_elo(session)
            await session.commit()

        await interaction.followup.send(
            f"Faceit ELO backfill complete.\n"
            f"Updated rows: **{updated}**\n"
            f"Skipped (no faceit_id): **{skipped}**\n"
            f"No ELO found for matchId: **{not_found}**",
            ephemeral=True,
        )

    @app_commands.command(
        name="fill_faceit_avg_elo",
        description="Backfill Faceit team & lobby average ELO per match where missing"
    )
    @system_admin_only()
    async def fill_faceit_avg_elo_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with SessionLocal() as session:
            async with session.begin():
                updated, skipped = await fill_faceit_avg_elo(session)
            await session.commit()

        await interaction.followup.send(
            f"Faceit lobby ELO backfill complete.\n"
            f"Updated matches: **{updated}**\n"
            f"Skipped: **{skipped}**",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(Util(bot))


