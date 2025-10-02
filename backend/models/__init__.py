# backend/models/__init__.py
from sqlalchemy import String, Integer, ForeignKey, UniqueConstraint, Float, BigInteger, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from ..db import Base
from datetime import datetime, timezone


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    discord_id: Mapped[int] = mapped_column(unique=True, index=True)
    teams: Mapped[list["Team"]] = relationship(back_populates="owner")
    steam_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)

class Player(Base):
    __tablename__ = "players"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    handle: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    faceit_id: Mapped[str | None] = mapped_column(String(64))
    premier_elo: Mapped[int | None]
    faceit_elo: Mapped[int | None]

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


