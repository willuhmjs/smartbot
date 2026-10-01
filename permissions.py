"""Guardrails: the bot only does what the *requesting user* is allowed to do.

Every MCP tool call is checked against the Discord permissions of the member who
pinged the bot, scoped to the target channel when one is given, plus role
hierarchy checks for moderation and role tools. Without this, anyone who can
@ the bot could have it ban people or delete channels.
"""

from typing import Any

import discord

# Tool name -> Discord permissions the requester must have.
# Tools not listed here (e.g. new ones added to discord-mcp later) require administrator.
TOOL_PERMISSIONS: dict[str, tuple[str, ...]] = {
    # Server info / lookups
    "get_server_info": (),
    "get_user_id_by_name": (),
    # DMs from the bot's account: restricted, since they act as the bot privately
    "send_private_message": ("manage_guild",),
    "edit_private_message": ("manage_guild",),
    "delete_private_message": ("manage_guild",),
    "read_private_messages": ("administrator",),
    # Messages (channel-scoped)
    "send_message": ("send_messages",),
    "edit_message": ("manage_messages",),
    "delete_message": ("manage_messages",),
    "read_messages": ("read_message_history",),
    "add_reaction": ("add_reactions",),
    "remove_reaction": ("manage_messages",),
    # Channels
    "create_text_channel": ("manage_channels",),
    "edit_text_channel": ("manage_channels",),
    "delete_channel": ("manage_channels",),
    "find_channel": (),
    "list_channels": (),
    "get_channel_info": (),
    "move_channel": ("manage_channels",),
    # Categories
    "create_category": ("manage_channels",),
    "edit_category": ("manage_channels",),
    "delete_category": ("manage_channels",),
    "find_category": (),
    "list_channels_in_category": (),
    # Webhooks
    "create_webhook": ("manage_webhooks",),
    "delete_webhook": ("manage_webhooks",),
    "list_webhooks": ("manage_webhooks",),
    "send_webhook_message": ("manage_webhooks",),
    # Roles
    "list_roles": (),
    "create_role": ("manage_roles",),
    "edit_role": ("manage_roles",),
    "delete_role": ("manage_roles",),
    "assign_role": ("manage_roles",),
    "remove_role": ("manage_roles",),
    # Moderation
    "kick_member": ("kick_members",),
    "ban_member": ("ban_members",),
    "unban_member": ("ban_members",),
    "timeout_member": ("moderate_members",),
    "remove_timeout": ("moderate_members",),
    "set_nickname": ("manage_nicknames",),
    "get_bans": ("ban_members",),
    # Voice / stage
    "create_voice_channel": ("manage_channels",),
    "create_stage_channel": ("manage_channels",),
    "edit_voice_channel": ("manage_channels",),
    "move_member": ("move_members",),
    "disconnect_member": ("move_members",),
    "modify_voice_state": ("mute_members", "deafen_members"),
    # Scheduled events
    "create_guild_scheduled_event": ("manage_events",),
    "edit_guild_scheduled_event": ("manage_events",),
    "delete_guild_scheduled_event": ("manage_events",),
    "list_guild_scheduled_events": (),
    "get_guild_scheduled_event_users": (),
    # Permission overwrites
    "list_channel_permission_overwrites": ("manage_roles",),
    "upsert_role_channel_permissions": ("manage_roles",),
    "upsert_member_channel_permissions": ("manage_roles",),
    "delete_channel_permission_overwrite": ("manage_roles",),
    # Invites
    "create_invite": ("create_instant_invite",),
    "list_invites": ("manage_guild",),
    "delete_invite": ("manage_guild",),
    "get_invite_details": (),
    # Forums
    "create_forum_channel": ("manage_channels",),
    "edit_forum_channel": ("manage_channels",),
    "list_forum_channels": (),
    "get_forum_channel_info": (),
    "list_forum_tags": (),
    "create_forum_post": ("send_messages",),
    "list_forum_posts": (),
    "modify_forum_post": ("manage_threads",),
    # Emojis
    "list_emojis": (),
    "get_emoji_details": (),
    "create_emoji": ("manage_expressions",),
    "edit_emoji": ("manage_expressions",),
    "delete_emoji": ("manage_expressions",),
}
UNKNOWN_TOOL_PERMISSIONS = ("administrator",)

# Tools whose target member must be below the requester in the role hierarchy.
MEMBER_TARGET_TOOLS = {
    "kick_member", "ban_member", "timeout_member", "remove_timeout", "set_nickname",
    "assign_role", "remove_role", "move_member", "disconnect_member", "modify_voice_state",
    "upsert_member_channel_permissions",
}
# Tools whose target role must be below the requester's top role.
ROLE_TARGET_TOOLS = {
    "edit_role", "delete_role", "assign_role", "remove_role", "upsert_role_channel_permissions",
}
# Tools that post content; mass pings are gated behind mention_everyone.
POSTING_TOOLS = {
    "send_message", "edit_message", "send_webhook_message", "send_private_message",
    "edit_private_message", "create_forum_post",
}

# Every interactive guild permission worth telling the model about.
SUMMARY_PERMISSIONS = (
    "administrator", "manage_guild", "manage_channels", "manage_roles", "manage_messages",
    "manage_webhooks", "manage_events", "manage_threads", "manage_nicknames", "manage_expressions",
    "kick_members", "ban_members", "moderate_members", "move_members", "mute_members",
    "mention_everyone", "create_instant_invite",
)


def _find_ids(args: dict[str, Any], *needles: str) -> list[int]:
    ids = []
    for key, value in args.items():
        k = key.lower()
        if not k.endswith("id") or not any(n in k for n in needles):
            continue
        try:
            ids.append(int(str(value).strip()))
        except (TypeError, ValueError):
            pass
    return ids


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _missing(perms: discord.Permissions, required: tuple[str, ...]) -> list[str]:
    if perms.administrator:
        return []
    return [p for p in required if not getattr(perms, p, False)]


def permission_summary(member: discord.Member) -> str:
    perms = member.guild_permissions
    if member.guild.owner_id == member.id:
        return "server owner (everything)"
    if perms.administrator:
        return "administrator (everything)"
    granted = [p for p in SUMMARY_PERMISSIONS if getattr(perms, p, False)]
    return ", ".join(granted) or "no moderation/management permissions"


async def check_tool_permission(
    tool: str,
    args: dict[str, Any],
    author: discord.Member,
    owner_ids: frozenset[int],
) -> str | None:
    """Return None if allowed, otherwise a human-readable reason for the denial."""
    if author.id in owner_ids:
        return None

    guild = author.guild
    is_guild_owner = guild.owner_id == author.id
    required = TOOL_PERMISSIONS.get(tool, UNKNOWN_TOOL_PERMISSIONS)

    # Channel-scoped check: channel overwrites can grant or deny per channel.
    channel_ids = _find_ids(args, "channel", "thread", "post")
    if channel_ids:
        for cid in channel_ids:
            channel = guild.get_channel_or_thread(cid)
            if channel is None:
                # Might be an un-cached archived thread; fall back to an API lookup.
                try:
                    channel = await guild.fetch_channel(cid)
                except discord.HTTPException:
                    return f"channel {cid} does not exist in this server"
            perms = channel.permissions_for(author)
            missing = _missing(perms, ("view_channel",) + required)
            if missing and not is_guild_owner:
                return f"you lack {', '.join(missing)} in #{channel.name}"
    else:
        missing = _missing(author.guild_permissions, required)
        if missing and not is_guild_owner:
            return f"you lack the {', '.join(missing)} permission"

    if is_guild_owner:
        return None

    # Role hierarchy: can't act on members at or above you.
    if tool in MEMBER_TARGET_TOOLS:
        for uid in _find_ids(args, "user", "member"):
            if uid == guild.owner_id:
                return "the server owner can't be targeted"
            if uid == author.id and tool == "set_nickname":
                continue
            target = guild.get_member(uid)
            if target is None:
                try:
                    target = await guild.fetch_member(uid)
                except discord.HTTPException:
                    continue  # not a member (e.g. banning by ID); Discord enforces the rest
            if target.top_role >= author.top_role:
                return f"{target.display_name}'s highest role is not below yours"

    if tool in ROLE_TARGET_TOOLS:
        for rid in _find_ids(args, "role"):
            role = guild.get_role(rid)
            if role is not None and role >= author.top_role:
                return f"role @{role.name} is not below your highest role"

    if tool in POSTING_TOOLS:
        text = " ".join(_strings(args))
        if ("@everyone" in text or "@here" in text or "<@&" in text) and not author.guild_permissions.mention_everyone:
            return "you lack mention_everyone, so the bot won't post @everyone/@here/role pings for you"

    return None
