# bot/bot.py
import os
import sys
import logging
import asyncio
from logging.handlers import RotatingFileHandler
import traceback

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

from backend.db import init_db

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = os.getenv("GUILD_ID") #fast guild-only sync
GUILD_OBJECT = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None

LOG_FILE = "logs/bot.log"

def setup_logging():
    os.makedirs("logs", exist_ok=True)
    fmt = "[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # console
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(fmt, datefmt))

    # rotating file so it doesnt get too big
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(fmt, datefmt))

    root.handlers.clear()
    root.addHandler(ch)
    root.addHandler(fh)

setup_logging()
log = logging.getLogger("fantasy-bot")

intents = discord.Intents.all()
intents.members = True


class FantasyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def load_all_cogs(self):
        import os
        cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
        base_pkg = "bot.cogs"

        for file in os.listdir(cogs_dir):
            if file.endswith(".py") and not file.startswith("_"):
                ext = f"{base_pkg}.{file[:-3]}"  # e.g. bot.cogs.util
                try:
                    await self.load_extension(ext)
                    log.info("Loaded cog: %s", ext)
                except Exception:
                    log.exception("Failed to load cog %s", ext)

    async def setup_hook(self):
        # init DB first
        await init_db()

        # load cogs
        await self.load_all_cogs()

        # sync slash commands
        try:
            if GUILD_OBJECT:
                synced = await self.tree.sync(guild=GUILD_OBJECT)
                log.info("Slash commands synced to guild %s: %d", GUILD_ID, len(synced))
            else:
                synced = await self.tree.sync()
                log.info("Global slash commands synced: %d (may take a few minutes to appear)", len(synced))
        except Exception:
            log.exception("Slash command sync failed")


bot = FantasyBot()


@bot.event
async def on_ready():
    log.info("READY: %s#%s (latency %.3fs)", bot.user.name, bot.user.discriminator, bot.latency)


# Command logging
@bot.event
async def on_interaction(interaction: discord.Interaction):
    """
    Logs every *attempted* application command invocation.
    Then lets discord.py process it normally.
    """
    try:
        if interaction.type == discord.InteractionType.application_command and interaction.command:
            user = f"{interaction.user} ({interaction.user.id})"
            guild = f"{interaction.guild.name}" if interaction.guild else "DM"
            channel = f"{interaction.channel} ({getattr(interaction.channel, 'id', 'n/a')})"
            cmd = interaction.command.qualified_name  # e.g. "team show"
            log.info("RUN: %s | %s | %s | /%s", user, guild, channel, cmd)
    except Exception:
        log.exception("Failed pre-invoke log")
    return True


@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command: app_commands.Command):
    """Logs successful completions."""
    try:
        user = f"{interaction.user} ({interaction.user.id})"
        guild = f"{interaction.guild.name}" if interaction.guild else "DM"
        channel = f"{interaction.channel} ({getattr(interaction.channel, 'id', 'n/a')})"
        cmd = command.qualified_name
        log.info("OK:  %s | %s | %s | /%s", user, guild, channel, cmd)
    except Exception:
        log.exception("Failed logging completion")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Logs slash command errors + traceback and notifies the user generically."""
    try:
        user = f"{interaction.user} ({interaction.user.id})" if interaction and interaction.user else "n/a"
        guild = f"{interaction.guild.name} ({interaction.guild_id})" if interaction and interaction.guild else "DM/n-a"
        channel = f"{interaction.channel} ({getattr(interaction.channel, 'id', 'n/a')})" if interaction else "n/a"
        cmd = getattr(interaction.command, "qualified_name", "unknown") if interaction else "unknown"
        log.error("ERR: %s | %s | %s | /%s | %r", user, guild, channel, cmd, error)
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        log.error("TRACE:\n%s", tb)

        # user-facing message
        if interaction:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Something went wrong running that command. I've stored it in the logs and if alfie isn't lazy he'll check (it'll never be looked at)", ephemeral=True)
                else:
                    await interaction.followup.send("Something went wrong running that command. I've stored it in the logs and if alfie isn't lazy he'll check (it'll never be looked at)", ephemeral=True)
            except Exception:
                pass
    except Exception:
        log.exception("Failed logging app command error")


def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN not set in .env")
    asyncio.run(bot.start(TOKEN))


if __name__ == "__main__":
    main()
