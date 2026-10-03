"""Slash commands: /settings (Manage Server) and /strikes (Moderate Members).

Discord hides each group from members without its default permission (server admins can change that in
Server Settings > Integrations), and every command checks the member's permissions again itself.
"""

import json
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from controls import Denied
from profiles import ProfileError
from settings import SECTIONS, SETTINGS, SettingError, editable_keys

if TYPE_CHECKING:
    from bot import SmartBot

log = logging.getLogger("smartbot.commands")


async def _reply(interaction: discord.Interaction, text: str) -> None:
    text = text if len(text) <= 1900 else text[:1900] + "…"
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def _run(interaction: discord.Interaction, coro) -> None:
    """Await a controls call and reply with its result or the reason it was refused."""
    try:
        text = await coro
    except Denied as e:
        text = f"🚫 Not allowed: {e}."
    except (SettingError, ProfileError) as e:
        text = f"⚠️ {e}"
    except Exception:  # noqa: BLE001
        log.exception("Command failed")
        text = "Something went wrong; check the bot's log."
    await _reply(interaction, text)


async def _call(fn, *args):
    """Run a plain function inside a coroutine, so _run reports its errors too."""
    return fn(*args)


class TextModal(discord.ui.Modal):
    """A multi-line editor for long text settings (rules, persona, bio)."""

    def __init__(self, bot: "SmartBot", key: str, current: str):
        super().__init__(title=f"Edit {key}")
        self.bot, self.key = bot, key
        self.text = discord.ui.TextInput(label=SETTINGS[key].help[:45], style=discord.TextStyle.paragraph,
                                         default=(current or "")[:4000], required=False,
                                         max_length=min(SETTINGS[key].max_len or 4000, 4000))
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await _run(interaction, self.bot.controls.change(interaction.user, self.key, self.text.value))


def setup_commands(bot: "SmartBot") -> app_commands.CommandTree:
    tree = app_commands.CommandTree(bot)
    controls = bot.controls

    settings = app_commands.Group(name="settings", description="View and change my settings in this server",
                                  guild_only=True, default_permissions=discord.Permissions(manage_guild=True))
    strikes = app_commands.Group(name="strikes", description="Members' strikes in this server",
                                 guild_only=True, default_permissions=discord.Permissions(moderate_members=True))

    async def key_choices(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not isinstance(interaction.user, discord.Member):
            return []
        keys = [k for k in editable_keys(interaction.user, bot.cfg.owner_ids) if current.lower() in k]
        return [app_commands.Choice(name=f"{k} ({SETTINGS[k].section})", value=k) for k in keys[:25]]

    async def value_choices(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        s = SETTINGS.get(getattr(interaction.namespace, "key", "") or "")
        if s is None:
            return []
        if s.kind == "bool":
            options = ["on", "off"]
        elif s.kind == "choice":
            options = list(s.choices)
        elif s.kind == "model":
            options = await controls.models()
        elif s.kind in ("tiers", "escalation") and interaction.guild:
            options = [json.dumps(bot.profiles.get(interaction.guild.id, s.key))]
        else:
            return []
        return [app_commands.Choice(name=o[:100], value=o[:100]) for o in options if current.lower() in o.lower()][:25]

    @settings.command(name="view", description="Show my settings here")
    @app_commands.choices(section=[app_commands.Choice(name=s, value=s) for s in SECTIONS])
    async def view(interaction: discord.Interaction, section: app_commands.Choice[str] | None = None):
        embed = discord.Embed(title=f"My settings in {interaction.guild.name}",
                              description=controls.overview(interaction.guild, section.value if section else None)[:4000])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @settings.command(name="set", description="Change a setting")
    @app_commands.describe(key="The setting", value="The new value (channels/roles: mention them; lists: comma-separated)")
    @app_commands.autocomplete(key=key_choices, value=value_choices)
    async def set_(interaction: discord.Interaction, key: str, value: str):
        if key in SETTINGS and SETTINGS[key].kind == "image":
            await _reply(interaction, f"Use `/settings {key}` and attach the image.")
            return
        await interaction.response.defer(ephemeral=True)
        await _run(interaction, controls.change(interaction.user, key, value))

    @settings.command(name="reset", description="Undo a change made in Discord, back to the deployment's value or the default")
    @app_commands.autocomplete(key=key_choices)
    async def reset(interaction: discord.Interaction, key: str):
        await interaction.response.defer(ephemeral=True)
        await _run(interaction, controls.reset(interaction.user, key))

    def text_editor(key: str, description: str):
        @settings.command(name=key.replace("automod_", ""), description=description)
        async def edit(interaction: discord.Interaction):
            await interaction.response.send_modal(TextModal(bot, key, bot.profiles.get(interaction.guild.id, key)))
        return edit

    text_editor("automod_rules", "Edit the rules automod enforces")
    text_editor("persona", "Edit how I talk and what I know in this server")
    text_editor("bio", "Edit my 'About me' in this server")

    def image_setter(key: str):
        @settings.command(name=key, description=f"Set my {key} in this server (PNG, JPEG or GIF)")
        async def set_image(interaction: discord.Interaction, image: discord.Attachment):
            await interaction.response.defer(ephemeral=True)
            try:
                raw = await image.read()
            except discord.HTTPException:
                await _reply(interaction, "Couldn't download that attachment.")
                return
            mime = (image.content_type or "").split(";")[0]
            await _run(interaction, controls.change(interaction.user, key, None, image=(raw, mime)))
        return set_image

    image_setter("avatar")
    image_setter("banner")

    @settings.command(name="test-automod", description="See what automod would decide about a message")
    async def test_automod(interaction: discord.Interaction, message: str):
        await interaction.response.defer(ephemeral=True)
        await _run(interaction, controls.test_automod(interaction.user, message))

    @strikes.command(name="view", description="Show a member's active strikes")
    async def strikes_view(interaction: discord.Interaction, member: discord.Member):
        await _run(interaction, _call(controls.strikes_text, interaction.user, member))

    @strikes.command(name="add", description="Give a member strikes (may trigger a timeout, kick or ban)")
    @app_commands.describe(points="How many strikes (1-10)")
    async def strikes_add(interaction: discord.Interaction, member: discord.Member,
                          points: app_commands.Range[int, 1, 10], reason: str):
        await interaction.response.defer(ephemeral=True)
        await _run(interaction, controls.add_strikes(interaction.user, member, points, reason))

    @strikes.command(name="clear", description="Remove all of a member's strikes")
    async def strikes_clear(interaction: discord.Interaction, member: discord.Member):
        await _run(interaction, controls.clear_strikes(interaction.user, member))

    tree.add_command(settings)
    tree.add_command(strikes)
    return tree
