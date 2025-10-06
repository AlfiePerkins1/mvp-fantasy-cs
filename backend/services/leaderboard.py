from __future__ import annotations
from datetime import datetime, timezone

from typing import Optional
from sqlalchemy import select, func, or_, and_, desc, literal_column
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import Team, TeamPlayer, WeeklyPoints, User
from backend.services.leetify_api import week_start_london,  current_week_start_norm


def resolve_weeks(at_time: Optional[datetime] = None,
                  week_norm: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """
    Returns (week_local, week_norm):
      week_local: tz-aware Europe/London Monday 00:00 (for TeamPlayer effective window)
      week_norm : UTC-naive Monday 00:00 (for WeeklyPoints.week_start)
    """
    if week_norm is not None:
        # interpret provided week_norm as UTC-naive; convert back to London-aware
        week_norm_aware = week_norm.replace(tzinfo=timezone.utc)
        week_local = week_norm_aware.astimezone(week_start_london().tzinfo)  # Europe/London
        return week_local, week_norm
    # else derive from 'now'
    week_local = week_start_london(at_time)
    week_norm = current_week_start_norm(at_time)
    return week_local, week_norm


async def get_team_leaderboard(
    session: AsyncSession,
    guild_id: int,
    at_time: Optional[datetime] = None,
    week_start_norm: Optional[datetime] = None,  # optional override (UTC-naive)
    limit: int = 25,
    offset: int = 0,
):
    #Resolve the two week anchors
    week_local, week_norm = resolve_weeks(at_time, week_start_norm)

    #Active roster CTE (player_id == user_id for points)
    active_tp = (
        select(
            TeamPlayer.team_id,
            TeamPlayer.player_id.label("user_id"),
        )
        .join(Team, Team.id == TeamPlayer.team_id)
        .where(
            Team.guild_id == guild_id,
            TeamPlayer.effective_from_week <= week_local,
            or_(
                TeamPlayer.effective_to_week.is_(None),
                TeamPlayer.effective_to_week > week_local,
            ),
        )
        .cte("active_tp")
    )

    # Team totals
    team_totals_q = (
        select(
            Team.id.label("team_id"),
            Team.name.label("team_name"),
            Team.owner_id,
            func.coalesce(func.sum(WeeklyPoints.weekly_score), 0).label("points"),
        )
        .join(active_tp, active_tp.c.team_id == Team.id)
        .join(
            WeeklyPoints,
            and_(
                WeeklyPoints.user_id == active_tp.c.user_id,   # user_id == player_id
                WeeklyPoints.guild_id == guild_id,
                WeeklyPoints.week_start == week_norm,          # UTC-naive compare
            ),
            isouter=True,
        )
        .where(Team.guild_id == guild_id)
        .group_by(Team.id)
        .order_by(desc(literal_column("points")), Team.name.asc())
        .limit(limit)
        .offset(offset)
    )
    totals = (await session.execute(team_totals_q)).all()
    if not totals:
        return []

    team_ids = [r.team_id for r in totals]

    # Per-team breakdown
    breakdown_q = (
        select(
            active_tp.c.team_id,
            active_tp.c.user_id,
            func.coalesce(WeeklyPoints.weekly_score, 0).label("points"),
        )
        .select_from(active_tp)
        .join(
            WeeklyPoints,
            and_(
                WeeklyPoints.user_id == active_tp.c.user_id,
                WeeklyPoints.guild_id == guild_id,
                WeeklyPoints.week_start == week_norm,
            ),
            isouter=True,
        )
        .where(active_tp.c.team_id.in_(team_ids))
    )
    b_rows = (await session.execute(breakdown_q)).all()
    players_by_team: dict[int, list[tuple[int, float]]] = {}
    for team_id, user_id, pts in b_rows:
        players_by_team.setdefault(team_id, []).append((user_id, float(pts or 0)))

    #Owner Discord IDs (so can mention)
    owner_map = dict(
        (await session.execute(
            select(Team.id, User.discord_id)
            .join(User, User.id == Team.owner_id)
            .where(Team.id.in_(team_ids))
        )).all()
    )

    out = []
    for r in totals:
        out.append(
            {
                "team_id": r.team_id,
                "team_name": r.team_name,
                "owner_id": r.owner_id,
                "owner_discord_id": owner_map.get(r.team_id),
                "points": float(r.points or 0),
                "players": sorted(players_by_team.get(r.team_id, []), key=lambda x: -x[1]),
                "week_start_local": week_local,
                "week_start_norm": week_norm,
            }
        )
    return out