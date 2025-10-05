from __future__ import annotations
from datetime import datetime, timezone, date
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession


from backend.models import Team, TeamPlayer, Player, TeamWeekState
from backend.services.leetify_api import current_week_start_london, next_week_start_london, current_week_start_norm, next_week_start_norm

# config
INITIAL_BUDGET = 25000
TRANSFERS_PER_WEEK = 1


# Helpers

async def get_global_player_price(session: AsyncSession, player_id: int) -> float | None:
    return await session.scalar(select(Player.price).where(Player.id == player_id))

async def get_or_create_team_week_state(
        session: AsyncSession, guild_id: int, team_id: int, week_start: datetime
) -> TeamWeekState:

    row = await session.scalar(
        select(TeamWeekState).where(
            TeamWeekState.guild_id == guild_id,
            TeamWeekState.team_id == team_id,
            TeamWeekState.week_start == week_start
        )
    )

    if row is None:
        last = await session.scalar(
            select(TeamWeekState).where(
                TeamWeekState.guild_id == guild_id,
                TeamWeekState.team_id == team_id
            ).order_by(TeamWeekState.week_start.desc())
        )
        start_budget = last.budget_remaining if last else INITIAL_BUDGET
        row = TeamWeekState(
            guild_id=guild_id,
            team_id=team_id,
            week_start=week_start,
            budget_remaining=start_budget,
            transfers_used=0
        )
        session.add(row)
        await session.flush()
    return row


async def roster_count(session: AsyncSession, team_id: int) -> int:
    return await session.scalar(
        select(func.count()).select_from(TeamPlayer).where(TeamPlayer.team_id == team_id)
    )

async def already_on_team(session: AsyncSession, team_id: int, player_id: int) -> bool:
    n = await session.scalar(
        select(func.count()).select_from(TeamPlayer).where(TeamPlayer.team_id == team_id, TeamPlayer.player_id == player_id
        )
    )
    return bool(n)


async def buy_player(session, team_id: int, player_id: int, now: datetime):
    next_week = next_week_start_norm(now)
    exists_next = await session.scalar(
        select(func.count()).select_from(TeamPlayer).where(
            TeamPlayer.team_id == team_id,
            TeamPlayer.player_id == player_id,
            TeamPlayer.effective_from_week == next_week,
            or_(TeamPlayer.effective_to_week.is_(None), TeamPlayer.effective_to_week > next_week),
        )
    )
    if exists_next:
        return
    session.add(TeamPlayer(
        team_id=team_id,
        player_id=player_id,
        effective_from_week=next_week,
        effective_to_week=None,
    ))
    await session.flush()


async def sell_player(session, team_id: int, player_id: int, now: datetime) -> str | None:
    """
    Week-locked sell:
      - If a queued buy exists for next week (from=next_week, to=NULL): delete it.
      - Else, if an active interval spans next week: set effective_to_week=next_week.
      - Else: nothing to do (return None).
    """

    next_week = next_week_start_norm(now)
    print(f'Selling: Next week: {next_week}')
    # Either queued buy that hasn't taken effect yet -> cancel it (cleanest is delete)
    queued = await session.scalar(
        select(TeamPlayer).where(
            TeamPlayer.team_id == team_id,
            TeamPlayer.player_id == player_id,
            TeamPlayer.effective_from_week == next_week,
            TeamPlayer.effective_to_week.is_(None),
        ).limit(1)
    )
    print(f'Selling: CaseA PlayerID: {player_id},TeamID: {team_id},effective_from_week: {next_week} ')
    if queued:
        await session.delete(queued)
        await session.flush()
        return "cancel_queued_buy"

    # OR active interval that spans next week -> close interval at next_week
    active = await session.scalar(
        select(TeamPlayer).where(
            TeamPlayer.team_id == team_id,
            TeamPlayer.player_id == player_id,
            TeamPlayer.effective_from_week < next_week,
            or_(TeamPlayer.effective_to_week.is_(None), TeamPlayer.effective_to_week > next_week),
        ).limit(1)
    )
    print(f'Selling: CaseB PlayerID: {player_id},TeamID: {team_id},effective_from_week: {next_week} ')
    if active:
        active.effective_to_week = next_week
        await session.flush()
        return "scheduled_removal"

    # No interval to cancel or close
    return None

async def roster_for_week(session, team_id: int, week_start: datetime) -> list[int]:
    rows = await session.scalars(
        select(TeamPlayer.player_id).where(
            TeamPlayer.team_id == team_id,
            TeamPlayer.effective_from_week <= week_start,
            or_(TeamPlayer.effective_to_week.is_(None), TeamPlayer.effective_to_week > week_start),
        )
    )
    return list(rows)


async def transfer_count_for_next_week(session, team_id: int, now: datetime) -> int:
    this_week = current_week_start_london(now)
    next_week = next_week_start_london(now)
    current_ids = set(await roster_for_week(session, team_id, this_week))
    next_ids    = set(await roster_for_week(session, team_id, next_week))
    buys  = len(next_ids - current_ids)
    sells = len(current_ids - next_ids)
    return buys + sells


async def team_has_active_this_week(session, team_id: int, this_week: datetime) -> bool:
    """True if any rows are active for this_week => post-first-lock."""
    n = await session.scalar(
        select(func.count()).select_from(TeamPlayer).where(
            TeamPlayer.team_id == team_id,
            TeamPlayer.effective_from_week <= this_week,
            or_(TeamPlayer.effective_to_week.is_(None), TeamPlayer.effective_to_week > this_week),
        )
    )
    return (n or 0) > 0
