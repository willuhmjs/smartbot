"""Changing the bot's settings and strikes from Discord, shared by the slash commands and the agent's tools.

Every operation checks the member's own permissions: Manage Server to change settings (plus whatever a
value needs, see settings.py) and Moderate Members for strikes, with the role hierarchy respected. The agent's
versions of these tools are only offered to members who pass the base check, and are checked again per call.
"""

import json
import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiohttp
import discord

from moderation import Verdict
from profiles import ProfileError
from settings import (SECTIONS, SETTINGS, SettingError, describe, editable_keys, missing_permissions, parse)

if TYPE_CHECKING:
    from bot import SmartBot

log = logging.getLogger("smartbot.controls")

IMAGE_HOSTS = ("cdn.discordapp.com", "media.discordapp.net")


class Denied(Exception):
    pass


def _is_boss(bot: "SmartBot", member: discord.Member) -> bool:
    return member.id in bot.cfg.owner_ids or member.id == member.guild.owner_id


def can_configure(bot: "SmartBot", member: discord.Member) -> bool:
    p = member.guild_permissions
    return _is_boss(bot, member) or p.administrator or p.manage_guild


def can_moderate(bot: "SmartBot", member: discord.Member) -> bool:
    p = member.guild_permissions
    return _is_boss(bot, member) or p.administrator or p.moderate_members


class Controls:
    def __init__(self, bot: "SmartBot"):
        self.bot = bot
        self._models: tuple[float, list[str]] = (0.0, [])

    # ---------- models ----------

    async def models(self) -> list[str]:
        """Models the LLM server offers (cached for 10 minutes); empty if it doesn't say."""
        if time.monotonic() - self._models[0] < 600 and self._models[1]:
            return self._models[1]
        try:
            page = await self.bot.agent.client.models.list()
            names = sorted(m.id for m in page.data)
        except Exception as e:  # noqa: BLE001
            log.warning("Couldn't list models: %s", e)
            return self._models[1]
        self._models = (time.monotonic(), names)
        return names

    # ---------- settings ----------

    def overview(self, guild: discord.Guild, section: str | None = None) -> str:
        profiles = self.bot.profiles
        lines = []
        for sec in SECTIONS:
            if section and sec != section:
                continue
            lines.append(f"**{sec}**")
            for key, s in SETTINGS.items():
                if s.section != sec or not s.editable:
                    continue
                value = profiles.get(guild.id, key)
                if key == "model" and not value:
                    value = f"{self.bot.cfg.llm_model} (bot default)"
                if key == "automod_model" and not value:
                    value = f"{self.bot.cfg.automod_model} (bot default)"
                if key == "access" and not value:
                    value = f"{profiles.access(guild.id, self.bot.cfg.access, self.bot.cfg.allowed_role_ids)[0]}"
                source = profiles.source(guild.id, key)
                lines.append(f"`{key}`: {describe(key, value, guild)}" + (f" _({source})_" if source != "default" else ""))
        return "\n".join(lines)

    def settings_json(self, guild: discord.Guild) -> dict[str, Any]:
        out = {}
        for key, s in SETTINGS.items():
            if s.editable:
                value = self.bot.profiles.get(guild.id, key)
                if s.kind == "image" and value:
                    value = "custom image" if not str(value).startswith("http") else value
                out[key] = {"value": value, "source": self.bot.profiles.source(guild.id, key)}
        out["model"]["effective"] = self.bot.profiles.get(guild.id, "model") or self.bot.cfg.llm_model
        out["automod_model"]["effective"] = self.bot.profiles.get(guild.id, "automod_model") or self.bot.cfg.automod_model
        return out

    async def change(self, member: discord.Member, key: str, raw: Any, image: tuple[bytes, str] | None = None) -> str:
        """Set a setting for the member's server. Raises Denied or SettingError/ProfileError with a reason."""
        if not can_configure(self.bot, member):
            raise Denied("changing my settings needs the Manage Server permission")
        guild = member.guild
        s = SETTINGS.get(key)
        if s is None or not s.editable:
            raise SettingError(f"unknown setting {key!r}; settings: {', '.join(k for k, v in SETTINGS.items() if v.editable)}")

        if s.kind == "image":
            value = None
            if image is None and raw not in (None, "", "none", "null"):
                image = await self._download(str(raw))
            if image is not None:
                value = self.bot.profiles.save_image(*image)
        else:
            value = parse(key, raw, guild, await self.models() if s.kind == "model" else None)

        missing = missing_permissions(member, key, value, self.bot.cfg.owner_ids)
        if missing:
            raise Denied(f"setting {key} to that needs {', '.join(missing)}, which you don't have")
        if key == "modlog_channel" and value and not _is_boss(self.bot, member):
            channel = guild.get_channel_or_thread(value)
            if not channel.permissions_for(member).view_channel:
                raise Denied("you can't see that channel, so you can't make it the mod log")

        self.bot.profiles.set(guild.id, key, value, by=member.id)
        shown = describe(key, value, guild)
        log.info("SETTING %s (%s) in %s: %s = %s", member, member.id, guild.name, key, shown)
        await self.bot.moderator.log(guild, f"⚙️ {member.mention} set `{key}` to {shown}")
        if key in ("nickname", "avatar", "banner", "bio"):
            await self.bot.profiles.apply(self.bot, guild)
        return f"`{key}` is now {shown}."

    async def reset(self, member: discord.Member, key: str) -> str:
        if not can_configure(self.bot, member):
            raise Denied("changing my settings needs the Manage Server permission")
        if key not in SETTINGS or not SETTINGS[key].editable:
            raise SettingError(f"unknown setting {key!r}")
        guild = member.guild
        if key not in self.bot.profiles.overrides.get(guild.id, {}):
            return f"`{key}` wasn't changed from Discord; it's {describe(key, self.bot.profiles.get(guild.id, key), guild)}."
        # Going back to the old value can need permissions too (e.g. a deployment escalation that bans).
        restored = self.bot.profiles.get(guild.id, key, discord_changes=False)
        missing = missing_permissions(member, key, restored, self.bot.cfg.owner_ids)
        if missing:
            raise Denied(f"going back to the previous {key} needs {', '.join(missing)}")
        self.bot.profiles.reset(guild.id, key)
        shown = describe(key, restored, guild)
        await self.bot.moderator.log(guild, f"⚙️ {member.mention} reset `{key}` (now {shown})")
        if key in ("nickname", "avatar", "banner", "bio"):
            await self.bot.profiles.apply(self.bot, guild)
        return f"`{key}` is back to {shown}."

    async def _download(self, url: str) -> tuple[bytes, str]:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in IMAGE_HOSTS:
            raise SettingError("images must be uploaded to Discord (attach the image to your message)")
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    raise SettingError(f"couldn't download the image (HTTP {resp.status})")
                raw = await resp.content.read(10 * 1024 * 1024 + 1)
                return raw, resp.content_type

    # ---------- strikes ----------

    def _check_target(self, member: discord.Member, target: discord.Member) -> None:
        if not can_moderate(self.bot, member):
            raise Denied("strikes need the Moderate Members permission")
        if _is_boss(self.bot, member):
            return
        if target.id == member.guild.owner_id:
            raise Denied("the server owner can't get strikes")
        if target.top_role >= member.top_role:
            raise Denied(f"{target.display_name}'s highest role is not below yours")

    async def add_strikes(self, member: discord.Member, target: discord.Member, points: int, reason: str) -> str:
        self._check_target(member, target)
        if not 1 <= points <= 10:
            raise SettingError("add 1 to 10 strikes at a time")
        # Strikes can trigger escalation, so the member needs the permissions the next steps use.
        guild = member.guild
        expiry = self.bot.profiles.get(guild.id, "strike_expiry_days")
        before = self.bot.moderator.strikes.total(guild.id, target.id, expiry)
        steps = [s for s in self.bot.profiles.get(guild.id, "escalation") or [] if before < s["strikes"] <= before + points]
        if steps:
            missing = missing_permissions(member, "escalation", steps, self.bot.cfg.owner_ids)
            missing = [m for m in missing if m != "manage_guild"]
            if missing:
                raise Denied(f"that many strikes would trigger a {steps[-1]['action']}, which needs {', '.join(missing)}")
        return await self.bot.moderator.add_strikes(guild, target, points, reason or "no reason given", by=member)

    def strikes_text(self, member: discord.Member, target: discord.Member) -> str:
        if not can_moderate(self.bot, member):
            raise Denied("strikes need the Moderate Members permission")
        guild = member.guild
        expiry = self.bot.profiles.get(guild.id, "strike_expiry_days")
        active = self.bot.moderator.strikes.active(guild.id, target.id, expiry)
        if not active:
            return f"{target.display_name} has no active strikes."
        lines = [f"<t:{int(ts)}:d> +{pts}: {reason}" for ts, pts, reason in active[-15:]]
        return f"{target.display_name} has {sum(a[1] for a in active)} active strike(s) (expire after {expiry} days):\n" + "\n".join(lines)

    async def clear_strikes(self, member: discord.Member, target: discord.Member) -> str:
        self._check_target(member, target)
        n = self.bot.moderator.strikes.clear(member.guild.id, target.id)
        await self.bot.moderator.log(member.guild, f"✅ {member.mention} cleared {target.mention}'s strikes")
        return f"Cleared {n} strike record(s) for {target.display_name}."

    async def test_automod(self, member: discord.Member, text: str) -> str:
        if not can_configure(self.bot, member):
            raise Denied("testing automod needs the Manage Server permission")
        verdict: Verdict | None = await self.bot.moderator.classify(member.guild, member.display_name, text)
        if verdict is None:
            return "The automod model didn't give a usable answer."
        if not verdict.violation:
            return f"No violation. {verdict.reason}"
        tier = self.bot.profiles.get(member.guild.id, "automod_tiers").get(verdict.severity, {})
        return (f"Violation ({verdict.severity}): **{verdict.rule}**. {verdict.reason}\n"
                f"Action here: {tier.get('action', 'log')}, +{tier.get('strikes', 0)} strike(s)"
                + ("" if self.bot.profiles.get(member.guild.id, "automod") else " (automod is off)"))

    # ---------- the agent's tools ----------

    def tool_specs(self, member: discord.Member) -> list[dict]:
        """OpenAI-format tools for the member: settings tools only with Manage Server, strike tools only
        with Moderate Members."""
        specs = []
        if can_configure(self.bot, member):
            keys = editable_keys(member, self.bot.cfg.owner_ids)
            catalog = "\n".join(f"- {k} ({SETTINGS[k].kind}): {SETTINGS[k].help}" for k in keys)
            specs += [
                _spec("get_bot_settings", "Show your (the bot's) current settings in this server, with where each "
                      "comes from (default, deployment, or set in Discord).", {}),
                _spec("update_bot_setting",
                      "Change one of your (the bot's) settings in this server, when a server manager asks. Settings "
                      f"this requester may change:\n{catalog}\nValues: bool true/false; channels and roles as IDs "
                      "(a list for plural kinds); text as a string; null to clear. automod_tiers is an object like "
                      '{"low": {"action": "log", "strikes": 0}, "medium": {"action": "delete_warn", "strikes": 1}, '
                      '"high": {"action": "delete_warn", "strikes": 2}}. escalation is a list like '
                      '[{"strikes": 3, "action": "timeout", "minutes": 60}, {"strikes": 6, "action": "ban"}]. '
                      "avatar/banner take the URL of an image attached to the request. For model, call "
                      "list_models first.",
                      {"key": {"type": "string", "enum": keys}, "value": {"description": "The new value"}},
                      ["key", "value"]),
                _spec("reset_bot_setting", "Undo a change made from Discord to one of your settings, going back to "
                      "the deployment's value or the default.", {"key": {"type": "string", "enum": keys}}, ["key"]),
                _spec("list_models", "List the AI models available for the model and automod_model settings.", {}),
                _spec("test_automod", "Check what automod would decide about a sample message in this server.",
                      {"text": {"type": "string"}}, ["text"]),
            ]
        if can_moderate(self.bot, member):
            user = {"userId": {"type": "string", "description": "The member's user ID"}}
            specs += [
                _spec("get_strikes", "Show a member's active strikes in this server.", user, ["userId"]),
                _spec("add_strikes", "Give a member strikes (they can trigger the server's escalation: timeout, kick "
                      "or ban).", {**user, "points": {"type": "integer", "minimum": 1, "maximum": 10},
                                   "reason": {"type": "string"}}, ["userId", "points", "reason"]),
                _spec("clear_strikes", "Remove all of a member's strikes in this server.", user, ["userId"]),
            ]
        return specs

    async def call_tool(self, name: str, args: dict, member: discord.Member) -> str:
        try:
            if name == "get_bot_settings":
                if not can_configure(self.bot, member):
                    raise Denied("needs Manage Server")
                return json.dumps(self.settings_json(member.guild))
            if name == "update_bot_setting":
                return await self.change(member, str(args.get("key")), args.get("value"))
            if name == "reset_bot_setting":
                return await self.reset(member, str(args.get("key")))
            if name == "list_models":
                if not can_configure(self.bot, member):
                    raise Denied("needs Manage Server")
                return json.dumps({"models": await self.models(), "bot_default": self.bot.cfg.llm_model,
                                   "automod_default": self.bot.cfg.automod_model})
            if name == "test_automod":
                return await self.test_automod(member, str(args.get("text", "")))
            target = await self._member(member.guild, args.get("userId"))
            if name == "get_strikes":
                return self.strikes_text(member, target)
            if name == "add_strikes":
                return await self.add_strikes(member, target, int(args.get("points", 1)), str(args.get("reason", "")))
            if name == "clear_strikes":
                return await self.clear_strikes(member, target)
        except Denied as e:
            return f"PERMISSION DENIED: {e}."
        except (SettingError, ProfileError, ValueError) as e:
            return f"ERROR: {e}"
        return f"ERROR: unknown tool {name}"

    async def _member(self, guild: discord.Guild, user_id: Any) -> discord.Member:
        try:
            uid = int(str(user_id).strip("<@!>"))
        except ValueError:
            raise SettingError("userId must be a user ID") from None
        target = guild.get_member(uid)
        if target is None:
            try:
                target = await guild.fetch_member(uid)
            except discord.HTTPException:
                raise SettingError(f"{uid} isn't a member of this server") from None
        return target


LOCAL_TOOLS = ("get_bot_settings", "update_bot_setting", "reset_bot_setting", "list_models", "test_automod",
               "get_strikes", "add_strikes", "clear_strikes")


def _spec(name: str, description: str, props: dict, required: list[str] | None = None) -> dict:
    params: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}
