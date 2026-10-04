"""Keeps every tool call inside the server it was asked from.

The bot is in several servers, and discord-mcp acts on whatever IDs it's given: a channel ID from another
server would work. So before any discord-mcp tool runs, every ID in its arguments is checked against the
requester's server, for everyone (bot owners included, since a prompt-injected model can act for them
too). This runs in code, after the model has chosen its arguments, so nothing in a prompt can get around it.

Any ID argument this module doesn't know how to check is refused, so a new discord-mcp tool or parameter is
blocked until it's handled here.
"""

import re
from typing import Any

import discord

_SNOWFLAKE = re.compile(r"\d{15,21}")

# Argument names (lowercased) by what they refer to.
_CHANNEL = re.compile(r"(channel|thread|post|parent|category)ids?$")
_ROLE = re.compile(r"roleids?$")
_USER = re.compile(r"(user|author|target|member)ids?$")
# Things inside a channel: fine once the channel is checked (or, for DMs, the user).
_IN_CHANNEL = {"messageid", "messageids", "replytomessageid", "answerid", "attachmentid", "tagid", "tagids",
               "appliedtagids"}
# Looked up through the guildId the bot fills in, so Discord keeps them to this server.
_BY_GUILD = {"emojiid", "stickerid", "soundid", "eventid", "ruleid", "integrationid", "templatecode"}
# Message content Discord validates itself (a bot can only send stickers it may use).
_CONTENT = {"stickerids"}
_WEBHOOK_URL = re.compile(r"/webhooks/(\d{15,21})/")


def _ids(value: Any) -> list[int]:
    return [int(i) for i in _SNOWFLAKE.findall(value if isinstance(value, str) else repr(value))]


def _is_id_param(key: str) -> bool:
    k = key.lower()
    return k.endswith(("id", "ids", "code")) or k == "webhookurl"


async def _channel_here(client: discord.Client, guild: discord.Guild, cid: int) -> bool:
    if guild.get_channel_or_thread(cid) is not None:
        return True
    try:  # e.g. an archived thread that isn't cached
        channel = await client.fetch_channel(cid)
    except (discord.HTTPException, discord.InvalidData):
        return False
    return getattr(getattr(channel, "guild", None), "id", None) == guild.id


async def _member_here(guild: discord.Guild, uid: int) -> bool:
    if guild.get_member(uid) is not None:
        return True
    try:
        await guild.fetch_member(uid)
        return True
    except discord.HTTPException:
        return False


async def check_scope(client: discord.Client, guild: discord.Guild, tool: str, args: dict[str, Any],
                      guild_params: set[str], has_guild_param: bool, is_bot_owner: bool) -> str | None:
    """None if every ID in `args` belongs to `guild`, otherwise why not."""
    if "app_emoji" in tool:
        # The bot's own emojis, not any server's: nothing to scope, but changing them affects every server.
        if tool.startswith("list_") or is_bot_owner:
            return None
        return "app emojis are shared by every server I'm in; only my owners can change them"
    dm_tool = "private_message" in tool
    channel_given = user_checked = False
    in_channel: list[str] = []
    for key, value in args.items():
        k = key.lower()
        if key in guild_params or not _is_id_param(key) or value in (None, "", []):
            continue
        if _CHANNEL.search(k):
            ids = _ids(value)
            if not ids:
                return f"{key} must be a channel ID"
            for cid in ids:
                if not await _channel_here(client, guild, cid):
                    return f"channel {cid} isn't in this server"
            channel_given = True
        elif _ROLE.search(k):
            for rid in _ids(value):
                if guild.get_role(rid) is None:
                    return f"role {rid} isn't in this server"
        elif _USER.search(k):
            if dm_tool:
                for uid in _ids(value):
                    if not await _member_here(guild, uid):
                        return f"user {uid} isn't a member of this server"
                user_checked = True
            # Otherwise the action is on this server (guildId) or a channel in it, checked above.
        elif k in _IN_CHANNEL:
            in_channel.append(key)
        elif k in _BY_GUILD:
            if not has_guild_param:
                return f"can't check that {key} belongs to this server"
        elif k == "webhookid" or k == "webhookurl":
            ids = _ids(value) if k == "webhookid" else [int(m) for m in _WEBHOOK_URL.findall(str(value))]
            if not ids:
                return f"{key} isn't a webhook"
            for wid in ids:
                try:
                    webhook = await client.fetch_webhook(wid)
                except discord.HTTPException:
                    return f"webhook {wid} isn't in this server"
                if webhook.guild_id != guild.id:
                    return f"webhook {wid} isn't in this server"
        elif k == "invitecode":
            try:
                invite = await client.fetch_invite(str(value))
            except discord.HTTPException:
                return f"invite {value} isn't for this server"
            if getattr(invite.guild, "id", None) != guild.id:
                return f"invite {value} isn't for this server"
        elif k in _CONTENT:
            continue
        else:
            return f"can't check that {key} belongs to this server"
    if in_channel and not channel_given and not (dm_tool and user_checked):
        return f"{in_channel[0]} needs a channel in this server alongside it"
    return None
