# backend/services/repo.py
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from ..models import User, Team, Player, TeamPlayer, ScoringConfig, PlayerStats

from backend.services.leetify_api import current_week_start_london
from datetime import datetime, timedelta, timezone

def _as_utc(dt: datetime) -> datetime:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)



async def get_or_create_user(session: AsyncSession, discord_id: int) -> User:
    res = await session.execute(select(User).where(User.discord_id == discord_id))
    user = res.scalar_one_or_none()
    if not user:
        user = User(discord_id=discord_id)
        session.add(user)
        await session.flush()
    return user

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

async def set_user_steam_id(session: AsyncSession, discord_id: int, steam_id: str) -> User:
    user = await get_or_create_user(session, discord_id)
    user.steam_id = steam_id
    await session.flush()
    return user

async def ensure_player_for_user(session: AsyncSession, user: User) -> Player:
    res = await session.execute(select(Player).where(Player.faceit_id == None, Player.handle == str(user.discord_id)))
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
) -> PlayerStats:

    """
    Inserts or updates PlayerStats for a given user in a guild
    """

    row = await get_cached_stats(session, user_id, guild_id)
    now = datetime.now(timezone.utc)
    if row:
        row.avg_leetify_rating = avg_leetify_rating
        row.sample_size = sample_size
        row.trade_kills = trade_kills
        row.ct_rating = ct_rating
        row.t_rating = t_rating
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
        fetched_at=now,
    )
    session.add(row)
    await session.flush()
    return row