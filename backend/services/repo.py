# backend/services/repo.py
from typing import Tuple, Optional
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, delete, insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.leetify_api import current_week_start_london
from backend.services.faceit_api import get_faceit_player_by_steam
from ..models import User, Team, Player, TeamPlayer, ScoringConfig, PlayerStats, PlayerGame


def _as_utc(dt: datetime) -> datetime:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

async def get_or_create_user(
    session: AsyncSession,
    discord_id: int,
    discord_username: str | None = None,
    discord_global_name: str | None = None,
    discord_display_name: str | None = None,
    discord_guild_id: int = None,
) -> User:
    print("test 1")
    print(f' Guild: {discord_guild_id}')
    res = await session.execute(select(User).where(User.discord_id == discord_id, User.discord_guild_id == discord_guild_id))
    user = res.scalar_one_or_none()

    print("test 2")
    if not user:
        print("test 2.5")
        user = User(discord_id=discord_id,
                    discord_guild_id = discord_guild_id
                    )
        print('test 2.75')
        print(f'{user}')
        session.add(user)
        await session.flush()
    print("test 3")
    # Update name fields if provided
    if discord_username is not None:
        user.discord_username = discord_username
    if discord_global_name is not None:
        user.discord_global_name = discord_global_name
    if discord_display_name is not None:
        user.discord_display_name = discord_display_name
    print("test 4")
    return user

# Read only, never creates user
async def get_user(session, *, discord_id: int, guild_id: int) -> User | None:
    return await session.scalar(
        select(User).where(
            User.discord_id == discord_id,
            User.discord_guild_id == guild_id,
        )
    )

# Write only
from sqlalchemy import select, func
from sqlalchemy.dialects.sqlite import insert

# Write-only: call this in /account register (or explicit linking), not in read paths.
async def create_user(
    session,
    *,
    discord_id: int,
    guild_id: int,
    steam_id: int | None = None,
    discord_username: str | None = None,
    discord_global_name: str | None = None,
    discord_display_name: str | None = None,
) -> User:
    assert guild_id is not None, "guild_id is required"

    # Only include non-None fields in VALUES
    values = {
        "discord_id": discord_id,
        "discord_guild_id": guild_id,
    }
    if steam_id is not None:
        values["steam_id"] = steam_id
    if discord_username is not None:
        values["discord_username"] = discord_username
    if discord_global_name is not None:
        values["discord_global_name"] = discord_global_name
    if discord_display_name is not None:
        values["discord_display_name"] = discord_display_name

    ins = insert(User).values(**values)

    # Only update columns that were provided; keep existing if new value is NULL
    update_set = {}
    if steam_id is not None:
        # keep existing steam_id if it's already set
        update_set["steam_id"] = func.coalesce(User.steam_id, ins.excluded.steam_id)
    if discord_username is not None:
        update_set["discord_username"] = ins.excluded.discord_username
    if discord_global_name is not None:
        update_set["discord_global_name"] = ins.excluded.discord_global_name
    if discord_display_name is not None:
        update_set["discord_display_name"] = ins.excluded.discord_display_name

    upsert = ins.on_conflict_do_update(
        # requires UNIQUE(discord_id, discord_guild_id)
        index_elements=[User.discord_id, User.discord_guild_id],
        set_=update_set,
    )

    await session.execute(upsert)

    # return the row
    return await session.scalar(
        select(User).where(
            User.discord_id == discord_id,
            User.discord_guild_id == guild_id,
        )
    )


async def get_or_create_player(session: AsyncSession, discord_id: int | str):
    """
    Ensure a Player row exists whose handle == discord_id (string).
    Updates the 'players' table with steamID
    """
    handle = str(discord_id)
    p = await session.scalar(select(Player).where(Player.handle == handle))
    if p:
        return p, False
    p = Player(handle=handle)  # inputs (elos) can be filled later by pricing/update
    session.add(p)
    # no commit here; caller's transaction will commit
    return p, True

async def remove_user_steam_id(
    session: AsyncSession, discord_id: int, *, purge_stats: bool = False, guild_id: Optional[int] = None) -> Tuple[User, Optional[str]]:
    user = await get_user(session, discord_id= discord_id, guild_id=guild_id)
    old = user.steam_id
    user.steam_id = None
    await session.flush()

    # Remove from teams (handle stored as the discord_id string)
    # Find Player rows that represent this Discord user
    player_ids = (await session.scalars(
        select(Player.id).where(Player.handle == str(discord_id))
    )).all()

    if player_ids:
        if guild_id is not None:
            # Find TeamPlayer rows for those players within this guild
            tp_ids = (await session.scalars(
                select(TeamPlayer.id)
                .join(Team, Team.id == TeamPlayer.team_id)
                .where(
                    Team.guild_id == guild_id,
                    TeamPlayer.player_id.in_(player_ids),
                )
            )).all()
        else:
            # All guilds
            tp_ids = (await session.scalars(
                select(TeamPlayer.id).where(TeamPlayer.player_id.in_(player_ids))
            )).all()

        if tp_ids:
            await session.execute(delete(TeamPlayer).where(TeamPlayer.id.in_(tp_ids)))
            await session.flush()

    # Optionally purge cached stats
    if purge_stats:
        if guild_id is not None:
            await session.execute(
                delete(PlayerStats).where(
                    PlayerStats.user_id == user.id,
                    PlayerStats.guild_id == guild_id,
                )
            )
        else:
            await session.execute(
                delete(PlayerStats).where(PlayerStats.user_id == user.id)
            )
        await session.flush()

    return user, old

async def create_team(session: AsyncSession, owner: User, name: str, guild_id: int) -> Team:
    team = Team(owner_id=owner.id, name=name, guild_id=guild_id)
    session.add(team)
    await session.flush()
    return team

async def add_player_by_handle(session: AsyncSession, team: Team, handle: str, role: str | None = None) -> TeamPlayer:
    res = await session.execute(select(Player).where(Player.handle == handle))
    player = res.scalar_one_or_none()
    if not player:
        player = Player(handle=handle)
        session.add(player)
        await session.flush()
    tp = TeamPlayer(team_id=team.id, player_id=player.id, role=role)
    session.add(tp)
    await session.flush()
    return tp

async def remove_player(session: AsyncSession, team: Team, handle: str) -> bool:
    q = (
        select(TeamPlayer)
        .join(Player, Player.id == TeamPlayer.player_id)
        .where(TeamPlayer.team_id == team.id, Player.handle == handle)
    )
    res = await session.execute(q)
    tp = res.scalar_one_or_none()
    if tp:
        await session.delete(tp)
        return True
    return False

async def get_or_create_scoring(session: AsyncSession) -> ScoringConfig:
    res = await session.execute(select(ScoringConfig).limit(1))
    cfg = res.scalar_one_or_none()
    if not cfg:
        cfg = ScoringConfig()
        session.add(cfg)
        await session.flush()
    return cfg

async def set_user_steam_id(session: AsyncSession, discord_id: int, steam_id: str, guild_id: int) -> User:
    user = await get_user(session, discord_id= discord_id, guild_id=guild_id)
    # faceit = await get_faceit_player_by_steam(steam_id)
    user.steam_id = steam_id
    # user.faceit = faceit
    await session.flush()
    return user

async def ensure_player_for_user(session: AsyncSession, user: User) -> Player:
    res = await session.execute(select(Player).where(Player.handle == str(user.discord_id)))
    p = res.scalar_one_or_none()
    if not p:
        p = Player(handle=str(user.discord_id))
        session.add(p)
        await session.flush()
    return p

CACHE_TTL = timedelta(hours=6)

async def get_cached_stats(session: AsyncSession, user_id: int, guild_id: int) -> PlayerStats | None:
    """
    Look up cached stats for a user in a guild if they exist
    """

    return await session.scalar(
        select(PlayerStats).where(
            PlayerStats.user_id == user_id,
            PlayerStats.guild_id == guild_id,
        )
    )

def is_stale(row: PlayerStats | None) -> bool:
    """
   Decide if the cached stats are stale, we'll refresh them if either no row exists, new week in london, or the row is older than 6 hours
    """
    if not row:
        return True

    week_start_utc = current_week_start_london().astimezone(timezone.utc)
    fetched = _as_utc(row.fetched_at)
    if fetched < week_start_utc:
        return True

    return (datetime.now(timezone.utc) - fetched) > CACHE_TTL

async def upsert_stats(
        session: AsyncSession,
        user_id: int,
        guild_id: int,
        avg_leetify_rating: float | None,
        sample_size: int | None,
        trade_kills: int | None,
        ct_rating: float | None,
        t_rating: float | None,
        adr: float | None,
        entries: float | None,
        flashes: float | None,
        util_dmg: float | None,
        faceit_games: int | None,
        premier_games: int | None,
        renown_games: int | None,
        mm_games: int | None,
        other_games: int | None,
        wins: int | None,
) -> PlayerStats:

    """
    Inserts or updates PlayerStats for a given user in a guild
    """

    print(f'Util {util_dmg}')
    print(f'Entries {entries}')
    print(f'Flashes {flashes}')

    row = await get_cached_stats(session, user_id, guild_id)
    now = datetime.now(timezone.utc)
    if row:
        row.avg_leetify_rating = avg_leetify_rating
        row.sample_size = sample_size
        row.trade_kills = trade_kills
        row.ct_rating = ct_rating
        row.t_rating = t_rating
        row.adr = adr
        row.entries = entries
        row.flashes = flashes
        row.util_dmg = util_dmg
        row.faceit_games = faceit_games
        row.premier_games = premier_games
        row.renown_games = renown_games
        row.mm_games = mm_games
        row.other_games = other_games
        row.wins = wins


        row.fetched_at = now
        await session.flush()
        return row

    row = PlayerStats(
        user_id=user_id,
        guild_id=guild_id,
        avg_leetify_rating=avg_leetify_rating,
        sample_size=sample_size,
        trade_kills=trade_kills,
        ct_rating=ct_rating,
        t_rating=t_rating,
        adr=adr,
        entries=entries,
        flashes=flashes,
        util_dmg=util_dmg,
        faceit_games=faceit_games,
        premier_games=premier_games,
        renown_games=renown_games,
        mm_games=mm_games,
        other_games=other_games,
        wins=wins,
        fetched_at=now
    )
    session.add(row)
    await session.flush()
    return row


async def user_by_discord_or_id(session, discord_id: int | str):
    return await session.scalar(
        select(User).where(User.discord_id == str(discord_id))
    )

async def player_by_handle(session, discord_id: int | str):
    return await session.scalar(
        select(Player).where(Player.handle == str(discord_id))
    )


async def leetify_l100_avg(session, user_id: int):

    steam = await session.scalar(
        select(User.steam_id).where(User.id == user_id)
    )
    if not steam:
        return None

    q = (
        select(PlayerGame.leetify_rating)
        .where(PlayerGame.steam_id == steam, PlayerGame.leetify_rating.isnot(None))
        .order_by(PlayerGame.finished_at.desc())
        .limit(100)
    )
    #print('User ID being used:', user_id)
    result = await session.execute(q)

    #print(f'Result: {result}')
    rows = [r[0] for r in result.all() if r[0] is not None]
    #print(f'Rows: {rows}')
    if rows:
        return float(sum(rows) / len(rows))

    # fallback if no leetify_rat1
    q2 = (
        select(PlayerGame.ct_leetify_rating, PlayerGame.t_leetify_rating)
        .where(PlayerGame.user_id == user_id)
        .order_by(PlayerGame.finished_at.desc())
        .limit(100)
    )
    result2 = await session.execute(q2)
    vals = []
    for ct, t in result2.all():
        if ct is not None and t is not None:
            vals.append((float(ct) + float(t)) / 2.0)
    return float(sum(vals) / len(vals)) if vals else None

async def upsert_player_ratings_and_l100(session, discord_id: str, *,
                                         renown_elo: int | None,
                                         premier_elo: int | None,
                                         faceit_elo: int | None,
                                         l100: float | None):
    p = await player_by_handle(session, discord_id)
    if not p:
        return False
    p.renown_elo = renown_elo
    p.premier_elo = premier_elo
    p.faceit_elo = faceit_elo
    p.leetify_l100_avg = l100
    p.price_updated_at = datetime.now(timezone.utc)
    session.add(p)
    return True
