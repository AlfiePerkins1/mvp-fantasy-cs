# backend/services/repo.py
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from ..models import User, Team, Player, TeamPlayer, ScoringConfig

async def get_or_create_user(session: AsyncSession, discord_id: int) -> User:
    res = await session.execute(select(User).where(User.discord_id == discord_id))
    user = res.scalar_one_or_none()
    if not user:
        user = User(discord_id=discord_id)
        session.add(user)
        await session.flush()
    return user

async def create_team(session: AsyncSession, owner: User, name: str) -> Team:
    team = Team(owner_id=owner.id, name=name)
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
