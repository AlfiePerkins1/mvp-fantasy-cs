# bot/bot.py
import os
import logging
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
from backend.db import init_db

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Optional: set your test server ID for instant (guild) slash commands
GUILD_ID = os.getenv("GUILD_ID")  # put the number in .env or leave unset for global
GUILD_OBJECT = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

# Logging so you SEE what's happening
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s')
log = logging.getLogger("fantasy-bot")

intents = discord.Intents.default()


class FantasyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # 1) Load cogs
        await self.load_all_cogs()

        # 2) Sync slash commands
        try:
            if GUILD_OBJECT:
                synced = await self.tree.sync(guild=GUILD_OBJECT)
                log.info(f"Slash commands synced to guild {GUILD_ID}: {len(synced)}")
            else:
                synced = await self.tree.sync()
                log.info(f"Global slash commands synced: {len(synced)} (may take a few minutes to appear)")
        except Exception as e:
            log.exception("Slash command sync failed: %s", e)

    async def load_all_cogs(self):
        # Load every .py file inside bot/cogs
        import os
        cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
        for file in os.listdir(cogs_dir):
            if file.endswith(".py") and not file.startswith("_"):
                ext = f"cogs.{file[:-3]}"
                try:
                    await self.load_extension(ext)
                    log.info(f"Loaded cog: {ext}")
                except Exception as e:
                    log.exception(f"Failed to load cog {ext}: {e}")

    async def setup_hook(self):
        # init database
        await init_db()
        await self.load_all_cogs()
        await self.tree.sync()


bot = FantasyBot()


@bot.event
async def on_ready():
    log.info(f"READY: {bot.user} (latency {bot.latency:.3f}s)")


def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set in .env")
    asyncio.run(bot.start(TOKEN))


if __name__ == "__main__":
    main()
