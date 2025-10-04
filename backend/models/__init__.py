# backend/models/__init__.py
from sqlalchemy import String, Integer, ForeignKey, UniqueConstraint, Float, BigInteger, DateTime, Boolean, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from ..db import Base
from datetime import datetime, timezone


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    discord_id: Mapped[int] = mapped_column(unique=True, index=True)
    teams: Mapped[list["Team"]] = relationship(back_populates="owner")
    steam_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    faceit_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)

class Player(Base):
    __tablename__ = "players"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    handle: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    renown_elo: Mapped[int | None]
    premier_elo: Mapped[int | None]
    faceit_elo: Mapped[int | None]
    leetify_l100_avg: Mapped[float | None]

    skill_score: Mapped[float | None] = mapped_column(Float)
    percentile: Mapped[float | None] = mapped_column(Float)
    price: Mapped[int | None] = mapped_column(Integer)
    price_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(64))
    owner: Mapped["User"] = relationship(back_populates="teams")
    players: Mapped[list["TeamPlayer"]] = relationship(back_populates="team", cascade="all, delete-orphan")
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    __table_args__ = (UniqueConstraint("owner_id", "guild_id", name="uq_owner_per_guild"),  # <= one team per user per server
                      UniqueConstraint("guild_id", "name", name="uq_teamname_per_guild"),   # <= Prevent duplicate teamnames
                      )
class TeamPlayer(Base):
    __tablename__ = "team_players"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    role: Mapped[str | None] = mapped_column(String(16))  # star, entry, awper, support, igl

    team: Mapped["Team"] = relationship(back_populates="players")
    player: Mapped["Player"] = relationship()

    __table_args__ = (UniqueConstraint("team_id", "player_id", name="uq_team_player"),)

class ScoringConfig(Base):
    __tablename__ = "scoring_config"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kill: Mapped[float] = mapped_column(Float, default=1.0)
    assist: Mapped[float] = mapped_column(Float, default=0.5)
    death: Mapped[float] = mapped_column(Float, default=-0.5)
    entry_kill: Mapped[float] = mapped_column(Float, default=2.0)
    win_bonus_per_5: Mapped[int] = mapped_column(Integer, default=10)
    star: Mapped[float] = mapped_column(Float, default=2.0)
    entry: Mapped[float] = mapped_column(Float, default=2.0)
    awper: Mapped[float] = mapped_column(Float, default=2.0)
    support: Mapped[float] = mapped_column(Float, default=1.2)
    igl: Mapped[float] = mapped_column(Float, default=1.1)

class PlayerStats(Base):
    __tablename__ = "player_stats"

    # Prim key
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Link to the user (who owns the steam account)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    #Per guild cache
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)

    # Cached Aggregates
    avg_leetify_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trade_kills: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ct_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    t_rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    adr: Mapped[float | None] = mapped_column(Float, nullable=True)
    entries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flashes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    util_dmg: Mapped[int | None] = mapped_column(Integer, nullable=True)

    faceit_games: Mapped[int | None] = mapped_column(Integer, nullable=True)
    premier_games: Mapped[int | None] = mapped_column(Integer, nullable=True)
    renown_games: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mm_games: Mapped[int | None] = mapped_column(Integer, nullable=True)
    other_games: Mapped[int | None] = mapped_column(Integer, nullable=True)

    wins: Mapped[int | None] = mapped_column(Integer, nullable=True)

    weekly_base_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    weekly_avg_mult: Mapped[float | None] = mapped_column(Float, nullable=True)
    weekly_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("user_id", "guild_id", name="uq_stats_user_guild"),
    )

class WeeklyPoints(Base):
    __tablename__ = "weekly_points"
    week_start = mapped_column(DateTime, primary_key=True)
    guild_id   = mapped_column(BigInteger, primary_key=True)
    user_id    = mapped_column(Integer, primary_key=True)

    ruleset_id  = mapped_column(Integer, nullable=False)
    computed_at = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    sample_size   = mapped_column(Integer, default=0)
    wins          = mapped_column(Integer, default=0)
    faceit_games  = mapped_column(Integer, default=0)
    premier_games = mapped_column(Integer, default=0)
    renown_games  = mapped_column(Integer, default=0)
    mm_games      = mapped_column(Integer, default=0)

    pts_rating = mapped_column(Float, default=0.0)
    pts_adr    = mapped_column(Float, default=0.0)
    pts_trades = mapped_column(Float, default=0.0)
    pts_entries= mapped_column(Float, default=0.0)
    pts_flashes= mapped_column(Float, default=0.0)
    pts_util   = mapped_column(Float, default=0.0)
    base_avg   = mapped_column(Float, default=0.0)

    avg_mult = mapped_column(Float, default=1.0)
    wr_eff   = mapped_column(Float, default=0.5)
    wr_mult  = mapped_column(Float, default=1.0)

    weekly_score = mapped_column(Float, default=0.0)


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Leetify meta
    data_source: Mapped[str] = mapped_column(String(32), index=True)          # 'faceit','premier','renown','matchmaking_competitive', ...
    source_match_id: Mapped[str] = mapped_column(String(64))                   # external match id
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    map_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    replay_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    has_banned_player: Mapped[bool] = mapped_column(Boolean, default=False)

    # Optional normalized team scores (helps win calc if you ever need it)
    team1_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team1_score:  Mapped[int | None] = mapped_column(Integer, nullable=True)
    team2_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    team2_score:  Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("data_source", "source_match_id", name="uq_match_source_matchid"),
        Index("idx_matches_source_finished", "data_source", "finished_at"),
    )


class PlayerGame(Base):
    """
    One row per (steam_id, match). Only store the tracked user's row,
    not all 10 players, to keep it lean.
    """
    __tablename__ = "player_games"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # link back to your Discord user (owner of steam account)
    user_id:  Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    steam_id: Mapped[str] = mapped_column(String(32), index=True)

    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    match:    Mapped[Match] = relationship(backref="player_rows")

    # convenient copy to filter by time without join
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data_source: Mapped[str] = mapped_column(String(32), index=True)

    # player-side stats (store Leetify ratings raw 0–1; scale when scoring)
    initial_team_number:    Mapped[int | None] = mapped_column(Integer, nullable=True)
    rounds_count:           Mapped[int | None] = mapped_column(Integer, nullable=True)
    rounds_won:             Mapped[int | None] = mapped_column(Integer, nullable=True)
    rounds_lost:            Mapped[int | None] = mapped_column(Integer, nullable=True)
    won:                    Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    leetify_rating:     Mapped[float | None] = mapped_column(Float)
    ct_leetify_rating:  Mapped[float | None] = mapped_column(Float)
    t_leetify_rating:   Mapped[float | None] = mapped_column(Float)

    total_kills:    Mapped[int | None] = mapped_column(Integer)
    total_deaths:   Mapped[int | None] = mapped_column(Integer)
    total_assists:  Mapped[int | None] = mapped_column(Integer)
    kd_ratio:       Mapped[float | None] = mapped_column(Float)

    dpr:                        Mapped[float | None] = mapped_column(Float)  # ADR
    he_foes_damage_avg:         Mapped[float | None] = mapped_column(Float)
    flashbang_leading_to_kill:  Mapped[int | None] = mapped_column(Integer)
    trade_kills_succeed:        Mapped[int | None] = mapped_column(Integer)

    # future: entries, roles, etc., add columns as Leetify exposes
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("steam_id", "match_id", name="uq_playergame_steam_match"),
        Index("idx_playergames_user_week", "user_id", "finished_at"),
        Index("idx_playergames_steam_week", "steam_id", "finished_at"),
    )
