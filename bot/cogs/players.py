import discord
from discord import app_commands
from discord.ext import commands

from typing import Optional

import io

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


from backend.db import SessionLocal

matplotlib.use("Agg")
from sqlalchemy import select, func, and_
from backend.models import User, WeeklyPoints


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

async def setup(bot: commands.Bot):
    await bot.add_cog(Players(bot))
