from __future__ import annotations
from datetime import datetime, timezone, timedelta

from typing import Optional
from sqlalchemy import select, func, or_, and_, desc, literal_column, cast, BigInteger
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Team, TeamPlayer, WeeklyPoints, User, Player
from backend.services.leetify_api import week_start_london,  current_week_start_norm
from bot.cogs.stats_refresh import week_bounds_naive_utc

def resolve_weeks(at_time: Optional[datetime] = None,
                  week_norm: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """
    Returns (week_local, week_norm):
      week_local: tz-aware Europe/London Monday 00:00 (for TeamPlayer effective window)
      week_norm : UTC-naive Monday 00:00 (for WeeklyPoints.week_start)
    """
    print(f' resolve weeks, week_local: week_norm: {week_norm}')
    if week_norm is not None:
        # interpret provided week_norm as UTC-naive; convert back to London-aware
        week_norm_aware = week_norm.replace(tzinfo=timezone.utc)
        week_local = week_norm_aware.astimezone(week_start_london().tzinfo)  # Europe/London
        return week_local, week_norm
    # else derive from 'now'
    week_local = week_start_london(at_time)
    week_norm = current_week_start_norm(at_time)
    print(f' resolve weeks, week_local: {week_local}, week_norm: {week_norm}')
    return week_local, week_norm

def week_key_naive_utc(week_local):
    # week_local is Monday 00:00 Europe/London (tz-aware)
    return (week_local
            .astimezone(timezone.utc)
            .replace(minute=0, second=0, microsecond=0, tzinfo=None))

from datetime import timedelta, timezone
from sqlalchemy import (
    select, and_, or_, func, desc, literal_column, cast, BigInteger
)
from sqlalchemy.orm import aliased

def week_key_naive_utc(week_local):
    # week_local is Monday 00:00 Europe/London (tz-aware)
    return (week_local.astimezone(timezone.utc)
                     .replace(minute=0, second=0, microsecond=0, tzinfo=None))

async def get_team_leaderboard(
    session: AsyncSession,
    guild_id: int,
    at_time: Optional[datetime] = None,
    week_start_norm: Optional[datetime] = None,  # optional override (UTC-naive)
    limit: int = 25,
    offset: int = 0,
):
    print(f'at_time? {at_time}')
    print('Provided week_start_norm:', week_start_norm)
    # Using new week start function for dates
    week_start_utc_naive, week_end_utc_naive = week_bounds_naive_utc("Europe/London")

    # Make the week anchors exactly like /team show
    # These ARE ONLY used for visuals, they DO NOT affect results
    week_local, _ = resolve_weeks(at_time, week_start_norm)
    week_norm_key = (week_start_norm.replace(minute=0, second=0, microsecond=0)
                     if week_start_norm is not None
                     else week_key_naive_utc(week_local))
    print(f'Week Norm: {week_norm_key}')

    U = aliased(User)

    # Active roster for the selected week: carry team metadata + player's Discord ID
    active_tp = (
        select(
            Team.id.label("team_id"),
            Team.name.label("team_name"),
            Team.owner_id.label("owner_id"),
            cast(Player.handle, BigInteger).label("discord_id"),  # handle is the Discord ID string
        )
        .join(TeamPlayer, TeamPlayer.team_id == Team.id)
        .join(Player, Player.id == TeamPlayer.player_id)
        .where(
            Team.guild_id == guild_id,
            TeamPlayer.effective_from_week < week_end_utc_naive,
            or_(
                TeamPlayer.effective_to_week.is_(None),
                TeamPlayer.effective_to_week > week_start_utc_naive,
            ),
        )
        .cte("active_tp")
    )

    # Totals per team (exact same week key equality as /team show)
    totals_q = (
        select(
            active_tp.c.team_id,
            active_tp.c.team_name,
            active_tp.c.owner_id,
            func.coalesce(func.sum(WeeklyPoints.weekly_score), 0).label("points"),
        )
        .join(U, and_(
            U.discord_id == active_tp.c.discord_id,
            U.discord_guild_id == guild_id,
        ))
        .join(
            WeeklyPoints,
            and_(
                WeeklyPoints.user_id == U.id,
                WeeklyPoints.guild_id == guild_id,
                WeeklyPoints.week_start == week_start_utc_naive,
            ),
            isouter=True,
        )
        .group_by(active_tp.c.team_id, active_tp.c.team_name, active_tp.c.owner_id)
        .order_by(desc(literal_column("points")), active_tp.c.team_name.asc())
        .limit(limit)
        .offset(offset)
    )
    totals = (await session.execute(totals_q)).all()
    if not totals:
        return []

    team_ids = [r.team_id for r in totals]

    # Per-team breakdown (optional, matches the mapping above)
    breakdown_q = (
        select(
            active_tp.c.team_id,
            U.id.label("user_id"),
            func.coalesce(WeeklyPoints.weekly_score, 0).label("points"),
        )
        .select_from(active_tp)
        .join(U, and_(
            U.discord_id == active_tp.c.discord_id,
            U.discord_guild_id == guild_id,
        ))
        .join(
            WeeklyPoints,
            and_(
                WeeklyPoints.user_id == U.id,
                WeeklyPoints.guild_id == guild_id,
                WeeklyPoints.week_start == week_start_utc_naive,
            ),
            isouter=True,
        )
        .where(active_tp.c.team_id.in_(team_ids))
    )
    b_rows = (await session.execute(breakdown_q)).all()
    players_by_team: dict[int, list[tuple[int, float]]] = {}
    for team_id, user_id, pts in b_rows:
        players_by_team.setdefault(team_id, []).append((user_id, float(pts or 0)))

    # Owners (for mentions)
    owner_map = dict(
        (await session.execute(
            select(Team.id, User.discord_id)
            .join(User, User.id == Team.owner_id)
            .where(Team.id.in_(team_ids))
        )).all()
    )

    out = []
    for r in totals:
        out.append({
            "team_id": r.team_id,
            "team_name": r.team_name,
            "owner_id": r.owner_id,
            "owner_discord_id": owner_map.get(r.team_id),
            "points": float(r.points or 0),
            "players": sorted(players_by_team.get(r.team_id, []), key=lambda x: -x[1]),
            "week_start_local": week_start_utc_naive,
            "week_start_norm": week_norm_key,
        })
    return out