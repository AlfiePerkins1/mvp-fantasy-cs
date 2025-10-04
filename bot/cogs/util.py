# bot/cogs/util.py
import discord
from discord import app_commands
from discord.ext import commands

def _is_admin_only(cmd: app_commands.Command | app_commands.Group) -> bool:
    dp = getattr(cmd, "default_permissions", None)
    return bool(dp and getattr(dp, "administrator", False))

def _flatten(commands_list, parents=None):
    """Yield tuples of (path_parts[list[str]], description, admin_only, obj)."""
    parents = parents or []
    for c in commands_list:
        if isinstance(c, app_commands.Group):
            # yield the group itself
            yield (parents + [c.name], c.description or "", _is_admin_only(c), c)
            # recurse into children
            yield from _flatten(list(c.commands), parents + [c.name])
        else:
            yield (parents + [c.name], c.description or "", _is_admin_only(c), c)

class Util(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="commands", description="Show all available slash commands for this bot, grouped.")
    async def list_commands(self, interaction: discord.Interaction):
        """
            Just lists the avaliable commands for the server

        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        all_cmds = list(self.bot.tree.get_commands())
        flat = list(_flatten(all_cmds))
        if not flat:
            await interaction.followup.send("No slash commands registered.", ephemeral=True)
            return

        # Group by top-level segment
        grouped: dict[str, list[tuple[str, str, bool]]] = {}
        for parts, desc, is_admin, obj in flat:
            top = parts[0] if parts else "misc"
            path_str = "/" + " ".join(parts)
            grouped.setdefault(top, []).append((path_str, desc, is_admin))

        # Desired display order; any others will follow alphabetically
        preferred = ["account", "leaderboard", "player", "pricing", "scoring", "team", "misc"]
        keys = [k for k in preferred if k in grouped] + sorted([k for k in grouped if k not in preferred])

        # Build embed
        embed = discord.Embed(
            title="Bot Commands",
            description="Grouped by top-level command. Admin-only commands are marked.",
            color=discord.Color.blurple()
        )

        for k in keys:
            nice = k.capitalize()
            # sort so the group root (e.g. "/account") is shown first
            items = sorted(grouped[k], key=lambda t: (t[0].count(" "), t[0]))
            # Build block text for this category; split into multiple fields if >1024 chars
            lines, fields = [], []
            for path, desc, is_admin in items:
                suffix = " **(admin only)**" if is_admin else ""
                # show the root ("/account") without a dash if it’s a group line
                if path.count(" ") == 0:
                    lines.append(f"`{path}` — {desc or 'No description'}{suffix}")
                else:
                    lines.append(f"• `{path}` — {desc or 'No description'}{suffix}")

            # chunk to respect 1024 char field limit
            buf = ""
            for line in lines:
                if len(buf) + len(line) + 1 > 1024:
                    fields.append(buf)
                    buf = line
                else:
                    buf += ("\n" if buf else "") + line
            if buf:
                fields.append(buf)
            for i, chunk in enumerate(fields, start=1):
                name = nice if i == 1 else f"{nice} (cont.)"
                embed.add_field(name=name, value=chunk, inline=False)

        embed.set_footer(text="Admin-only detected via default_permissions(administrator=True).")
        await interaction.followup.send(embed=embed, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Util(bot))

