import discord
from discord import app_commands
from discord.ext import commands
from discord import Color
from typing import Optional

import io

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


from backend.db import SessionLocal
from bot.cogs.leaderboard import NO_PINGS
from bot.cogs.stats_refresh import week_bounds_naive_utc

matplotlib.use("Agg")
from sqlalchemy import select, func, and_
from backend.models import User, WeeklyPoints, PlayerStats


class Players(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    player = app_commands.Group(name="player", description="Player-related commands")

    @player.command(name="graph")
    @app_commands.describe(member = "Target user (@mention)")
    async def graph(self, interaction: discord.Interaction, member: discord.User):
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this command in a server")
            return
        await interaction.response.defer(ephemeral=False, thinking=True)

        async with SessionLocal() as session:
            async with session.begin():

                user_id = await session.scalar(
                    select(User.id).where(
                        User.discord_id == int(member.id),
                        User.discord_guild_id == guild_id,
                    )
                )
                print(f'User ID: {user_id}')
                if not user_id:
                    await interaction.followup.send(f'{member.mention} is not registered in this server')
                    return

                latest = (
                    select(
                    WeeklyPoints.week_start,
                        func.max(WeeklyPoints.computed_at).label("latest_ts"),
                    )
                    .where(
                        WeeklyPoints.guild_id == guild_id,
                        WeeklyPoints.user_id == user_id,
                        WeeklyPoints.weekly_score.isnot(None),
                        WeeklyPoints.computed_at.isnot(None),
                    )
                    .group_by(WeeklyPoints.week_start)
                ).subquery()

                q = (
                    select(WeeklyPoints.week_start, WeeklyPoints.weekly_score)
                    .join(latest,
                          and_(
                              WeeklyPoints.week_start == latest.c.week_start,
                              WeeklyPoints.computed_at == latest.c.latest_ts,
                          ),
                    )
                    .where(
                        WeeklyPoints.guild_id == guild_id,
                        WeeklyPoints.user_id == user_id,
                    )
                    .order_by(WeeklyPoints.week_start.desc())
                )
                pts = (await session.execute(q)).all()

                if not pts:
                    await interaction.followup.send(f'No weekly points for {member.mention}')
                    return

                weeks = [ws for (ws,_) in pts]
                scores = [float(s) for (_,s) in pts]

                fig, ax = plt.subplots(figsize=(8, 4),dpi=200)
                ax.plot(weeks,scores, marker="o", linewidth=2)
                ax.set_title(f'Weekly Points | {member.display_name}', pad=10)
                ax.set_xlabel('Week Start')
                ax.set_ylabel('Points')
                ax.grid(True, alpha=0.3)

                ax.xaxis.set_major_locator(mdates.AutoDateLocator())
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
                fig.autofmt_xdate()

                fig.tight_layout()

                buf = io.BytesIO()
                fig.savefig(buf, format="png", bbox_inches="tight")
                plt.close(fig)
                buf.seek(0)

                file = discord.File(buf, filename="weekly_points.png")
                embed = discord.Embed(
                    title=f'Weekly Points | {member.display_name}',
                    description=f'Latest scores per gameweek',
                    colour=discord.Colour.green(),
                )
                embed.set_image(url="attachment://weekly_points.png")
                await interaction.followup.send(file=file, embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # @player.command(name="remove")
    # async def remove(self, interaction: discord.Interaction, name: str):
    #     await interaction.response.send_message(f"❌ Removed {name} from your team.")
    #
    # @player.command(name="info")
    # async def info(self, interaction: discord.Interaction, name: str):
    #     await interaction.response.send_message(f"📊 Stats for {name}: ...")

    @player.command(name="point_breakdown", description="See what is effecting a users points")
    @app_commands.describe(member="Target user (@mention)")
    async def points_breakdown(self, interaction: discord.Interaction, member: discord.User):
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("Use this command in a server")
            return
        await interaction.response.defer(ephemeral=False, thinking=True)

        async with SessionLocal() as session:
            async with session.begin():

                user_id = await session.scalar(
                    select(User.id).where(
                        User.discord_id == int(member.id),
                        User.discord_guild_id == guild_id,
                    )
                )
                if not user_id:
                    await interaction.followup.send(f'{member.mention} is not registered in this server')
                    return

                stats_row = await session.execute(
                    select(
                        PlayerStats.avg_leetify_rating,
                        PlayerStats.sample_size,
                        PlayerStats.wins,
                        PlayerStats.ct_rating,
                        PlayerStats.t_rating,
                        PlayerStats.adr,
                        PlayerStats.flashes,
                        PlayerStats.util_dmg,
                        PlayerStats.faceit_games,
                        PlayerStats.premier_games,
                        PlayerStats.renown_games,
                        PlayerStats.mm_games,
                        PlayerStats.other_games,
                    ).where(
                        PlayerStats.guild_id == guild_id,
                        PlayerStats.user_id == user_id,
                    )
                )
                s = stats_row.one_or_none()
                if not s:
                    await interaction.followup.send(f"No weekly stats found for {member.mention} in this server.")
                    return
                (
                    avg_rating, sample, wins,
                    ct_rating, t_rating, adr, flashes, util_dmg,
                    faceit_g, premier_g, renown_g, mm_g, other_g
                ) = s

                def f1(x):
                    try: return f"{float(x):.1f}"
                    except: return "—"

                def f2(x):
                    try: return f"{float(x):.2f}"
                    except: return "—"

                def fint(x):
                    try: return f"{int(x)}"
                    except: return "0"

                week_key, _ = week_bounds_naive_utc("Europe/London")
                latest_q = (
                    select(WeeklyPoints.weekly_score)
                    .where(
                        WeeklyPoints.guild_id == guild_id,
                        WeeklyPoints.user_id == user_id,
                        WeeklyPoints.week_start == week_key,
                        WeeklyPoints.weekly_score.isnot(None),
                        WeeklyPoints.computed_at.isnot(None),
                    )
                    .order_by(WeeklyPoints.computed_at.desc())
                    .limit(1)
                )
                weekly_total = await session.scalar(latest_q)

                left = [
                    f"**Avg Leetify**: {f2(avg_rating)}",
                    f"**Games**: {fint(sample)} ",
                    f"**Wins**: {fint(wins)}",
                    f"**CT rating**: {f2(ct_rating)}",
                    f"**T rating**: {f2(t_rating)}",
                    f"**ADR**: {f1(adr)}",
                    f"**Flashes**: {f1(flashes)}",
                    f"**Util dmg**: {f1(util_dmg)}",
                ]

                games = [
                    f"**Faceit**: {fint(faceit_g)}",
                    f"**Premier**: {fint(premier_g)}",
                    f"**Renown**: {fint(renown_g)}",
                    f"**Matchmaking**: {fint(mm_g)}",
                    f"**Other**: {fint(other_g)}",
                ]

                embed = discord.Embed(
                    title=f"Weekly breakdown — {member.display_name}",
                    description=f"Week starting {week_key:%Y-%m-%d}",
                    color=discord.Color.gold(),
                )

                if weekly_total is not None:
                    embed.add_field(name="Fantasy points (this week)", value=f"**{float(weekly_total):.1f}**",
                                    inline=False)

                embed.add_field(name="__**Performance**__", value="\n".join(left), inline=True)
                embed.add_field(name="__**Game Type**__", value="\n".join(games), inline=True)

                await interaction.followup.send(embed=embed, allowed_mentions=NO_PINGS)


async def setup(bot: commands.Bot):
    await bot.add_cog(Players(bot))
