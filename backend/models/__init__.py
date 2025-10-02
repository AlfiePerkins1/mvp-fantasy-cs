# backend/models/__init__.py
from sqlalchemy import String, Integer, ForeignKey, UniqueConstraint, Float, BigInteger
from sqlalchemy.orm import Mapped, mapped_column, relationship
from ..db import Base

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    discord_id: Mapped[int] = mapped_column(unique=True, index=True)
    teams: Mapped[list["Team"]] = relationship(back_populates="owner")

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

    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_owner_teamname"),)

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
