# bot/cogs/pricing.py
import discord
from discord import app_commands
from discord.ext import commands

from sqlalchemy import select, cast, BigInteger
from backend.db import SessionLocal


from backend.models import Player, User, PlayerStats
from backend.services.pricing import refresh_one_player, compute_and_persist_prices
from backend.services.repo import get_or_create_player
from backend.models import User




class Pricing(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    pricing = app_commands.Group(name="pricing", description="Manage and updated prices for players")

    @pricing.command(name="update", description="Refresh ratings for pricing")
    @app_commands.checks.has_permissions(administrator=True)
    async def update(self, interaction: discord.Interaction):
        """
            Admin Only
            Refreshes ratings and recalculates players pricing

            Iterates over all entries in the 'player' table.
            For each, it runs refresh_one_player() which updates their current ratings (Leetify, prem etc)
            Once all refreshed it calls compute_and_persist_prices() to recalculate fantasy prices based on rating percentiles

            Writes to via:
                refresh_one_player(): Player, PlayerStats(Maybe)
                compute_and_persist_prices(): Player


            Use to keep player prices up to date

        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        # First, list all handles with a read-only session
        async with SessionLocal() as s:
            handles = [h for h in (await s.execute(select(Player.handle))).scalars().all()]

        results = []
        for handle in handles:
            try:
                async with SessionLocal() as session:
                    # independent tx per player
                    r = await refresh_one_player(session, str(handle))
                    await session.commit()
                    results.append(r)
            except Exception as e:
                results.append({"discord_id": str(handle), "ok": False, "reason": str(e)})

        async with SessionLocal() as session:
            updated_prices = await compute_and_persist_prices(session)
            await session.commit()

        ok = sum(1 for r in results if r.get("ok"))
        fail = [r for r in results if not r.get("ok")]
        msg = f"Updated {ok}/{len(results)} players.\n"
        msg += f"Prices recalculated for {len(updated_prices)} players.\n"

        # show top 5 most expensive
        top = sorted(updated_prices, key=lambda x: x["price"], reverse=True)[:5]
        if top:
            msg += "\nTop priced players:\n"
            msg += "\n".join(
                f"- <@{u['handle']}> → {u['price']} (p={u['percentile']:.2f})"
                for u in top
            )

        if fail:
            msg += f"\nFailures ({len(fail)}):\n" + "\n".join(
                f"- <@{f['discord_id']}>: {f.get('reason', 'error')}" for f in fail[:10]
            )

        await interaction.followup.send(msg, ephemeral=True)


    #TODO:
    # Update pricing show command to only include players in the same guild
    @pricing.command(name="show", description="List all registered players and their prices (highest to lowest")
    @app_commands.describe(limit="How many players to show (1–50).", all_guilds="Include players from ALL guilds (default: only this server)")
    async def leaderboard(self, interaction: discord.Interaction, limit: int = 20, all_guilds: bool = False):

        guild = interaction.guild
        guild_id = interaction.guild_id
        if not guild_id and not all_guilds:
            await interaction.response.send_message("Use this in a server (or pass all_guilds=True).", ephemeral=True)
            return

        limit = max(1, min(50, limit))
        await interaction.response.defer(ephemeral=False, thinking=True)

        # fetch top N by price (ignore NULLs)
        async with SessionLocal() as session:

            if not all_guilds:

                result = await session.execute(
                    select(Player.handle, Player.price)
                    .join(User, cast(Player.handle, BigInteger) == User.discord_id)  # handle == discord_id
                    .where(
                        User.discord_guild_id == guild_id,
                        Player.price.is_not(None),
                    )
                    .order_by(Player.price.desc())
                    .limit(limit)
                )
                rows = result.all()
            else:
                result = await session.execute(
                    select(Player.handle, Player.price)
                    .join(User, cast(Player.handle, BigInteger) == User.discord_id)  # handle == discord_id
                    .where(Player.price.is_not(None),
                    )
                    .order_by(Player.price.desc())
                    .limit(limit)
                )
                rows = result.all()

        if not rows:
            await interaction.followup.send("No priced players yet. Run `/pricing update` first.", ephemeral=True)
            return

        # build a numbered list, mentioning users via their Discord IDs (handles)
        lines = []
        for i, (handle, price) in enumerate(rows, start=1):
            mention = f"<@{handle}>"  # handle is your stored discord_id
            lines.append(f"**{i}.** {mention} — **{price:,}**")

        embed = discord.Embed(
            title="Pricing",
            description="\n".join(lines),
            color=discord.Color.gold()
        )
        embed.set_footer(text=f"Top {len(rows)} players by price")

        await interaction.followup.send(embed=embed)

    @pricing.command(name="sync_players", description="Syncs users")
    @app_commands.checks.has_permissions(administrator=True)
    async def sync_players(self, interaction: discord.Interaction):
        """
            Admin only
            Syncs the databse with all registered users who have a steamID
            Looks up users in the 'Users' table (when steam_id exists)
            Calls get_or_create_player() for each ensuring there is a linked entry in the Players table.

            Use: Ran after new user does /register to make sure they're represented in the pricing system

        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        async with SessionLocal() as session:
            async with session.begin():
                users = (await session.execute(
                    select(User.discord_id).where(User.steam_id.is_not(None))
                )).scalars().all()

                added = 0
                for did in users:
                    _, created = await get_or_create_player(session, did)
                    if created:
                        added += 1
                        print(f'Added {did}')

        await interaction.followup.send(f"Sync complete. Added {added} player(s).", ephemeral=True)



async def setup(bot: commands.Bot):
    await bot.add_cog(Pricing(bot))
