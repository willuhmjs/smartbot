"""Guardrails: the bot only does what the *requesting user* is allowed to do.

Every MCP tool call is checked against the Discord permissions of the member who
pinged the bot, scoped to the target channel when one is given, plus role
hierarchy checks for moderation and role tools. Without this, anyone who can
@ the bot could have it ban people or delete channels.
"""

import json
import re
from typing import Any, Callable

import discord

def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _present(args: dict[str, Any], key: str) -> bool:
    return args.get(key) not in (None, "")


def _create_thread(args: dict[str, Any]) -> tuple[str, ...]:
    return ("create_private_threads",) if _truthy(args.get("private")) else ("create_public_threads",)


def _archived_threads(args: dict[str, Any]) -> tuple[str, ...]:
    base = ("read_message_history",)
    return base + ("manage_threads",) if _truthy(args.get("private")) else base


def _edit_member(args: dict[str, Any]) -> tuple[str, ...]:
    """edit_member bundles several actions; require the permission for each field that is set."""
    needs = {"nick": "manage_nicknames", "roleIds": "manage_roles", "timeoutUntil": "moderate_members",
             "mute": "mute_members", "deaf": "deafen_members", "voiceChannelId": "move_members"}
    required = tuple(perm for key, perm in needs.items() if _present(args, key))
    return required or ("manage_nicknames",)  # nothing to change: fail like the smallest action


def _channel_write(args: dict[str, Any]) -> tuple[str, ...]:
    # Permission overwrites need Manage Roles on top of Manage Channels (Discord's own rule).
    extra = _present(args, "overwritesJson") or _truthy(args.get("lockPermissions"))
    return ("manage_channels", "manage_roles") if extra else ("manage_channels",)


# Tool name -> Discord permissions the requester must have (or a function of the arguments).
# Tools not listed here (e.g. new ones added to discord-mcp later) require administrator.
TOOL_PERMISSIONS: dict[str, tuple[str, ...] | Callable[[dict[str, Any]], tuple[str, ...]]] = {
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
    # ---- Added by willuhmjs/discord-mcp (the 75 tools above keep their legacy names) ----
    # Messages
    "get_message": ("read_message_history",),
    "get_attachment": ("read_message_history",),
    "search_messages": ("read_message_history",),  # also needs a channelId, see CHANNEL_REQUIRED_TOOLS
    "list_pins": ("read_message_history",),
    "list_reactions": ("read_message_history",),
    "get_poll_voters": ("read_message_history",),
    "forward_message": ("send_messages", "read_message_history"),
    "pin_message": ("manage_messages",),
    "unpin_message": ("manage_messages",),
    "bulk_delete_messages": ("manage_messages",),
    "crosspost_message": ("manage_messages",),
    "clear_reactions": ("manage_messages",),
    "remove_user_reaction": ("manage_messages",),
    "end_poll": ("manage_messages",),
    "send_typing": ("send_messages",),
    "create_role_menu": ("manage_roles", "send_messages"),
    "list_interactions": (),
    # Channels
    "create_channel": _channel_write,
    "edit_channel": _channel_write,
    "follow_announcement_channel": ("manage_webhooks",),
    "list_channel_invites": ("manage_channels",),
    # Voice / stage
    "set_voice_channel_status": ("manage_channels",),
    "create_stage_instance": ("mute_members", "move_members"),
    "edit_stage_instance": ("mute_members", "move_members"),
    "delete_stage_instance": ("mute_members", "move_members"),
    # Forums
    "create_forum_tag": ("manage_threads",),
    "edit_forum_tag": ("manage_threads",),
    "delete_forum_tag": ("manage_threads",),
    # Threads
    "list_active_threads": ("read_message_history",),
    "create_thread": _create_thread,
    "edit_thread": ("manage_threads",),
    "add_thread_member": ("manage_threads",),
    "remove_thread_member": ("manage_threads",),
    "list_thread_members": ("read_message_history",),
    "join_thread": (),
    "leave_thread": (),
    "list_archived_threads": _archived_threads,
    # Members and moderation
    "get_member": (),
    "search_members": (),
    "list_members": (),
    "get_user": (),
    "edit_member": _edit_member,
    "bulk_ban": ("ban_members",),
    "get_ban": ("ban_members",),
    "prune_members": ("kick_members",),
    "set_bot_nickname": ("manage_nicknames",),
    # Roles
    "reorder_roles": ("manage_roles",),
    "get_role_member_counts": (),
    "get_role": (),
    "list_role_members": (),
    # Server settings
    "edit_server": ("manage_guild",),
    "get_welcome_screen": ("manage_guild",),
    "edit_welcome_screen": ("manage_guild",),
    "get_onboarding": ("manage_guild",),
    "edit_onboarding": ("manage_guild", "manage_roles"),
    "set_incident_actions": ("manage_guild",),
    "get_widget": ("manage_guild",),
    "edit_widget": ("manage_guild",),
    "get_vanity_url": ("manage_guild",),
    "list_integrations": ("manage_guild",),
    "delete_integration": ("manage_guild",),
    "list_voice_regions": (),
    "list_guild_templates": ("manage_guild",),
    "create_guild_template": ("manage_guild",),
    "sync_guild_template": ("manage_guild",),
    "edit_guild_template": ("manage_guild",),
    "delete_guild_template": ("manage_guild",),
    "get_audit_log": ("view_audit_log",),
    # AutoMod
    "list_automod_rules": ("manage_guild",),
    "get_automod_rule": ("manage_guild",),
    "create_automod_rule": ("manage_guild",),
    "edit_automod_rule": ("manage_guild",),
    "delete_automod_rule": ("manage_guild",),
    # Events
    "get_guild_scheduled_event": (),
    # Webhooks
    "get_webhook": ("manage_webhooks",),
    "edit_webhook": ("manage_webhooks",),
    "list_guild_webhooks": ("manage_webhooks",),
    "get_webhook_message": ("manage_webhooks",),
    "edit_webhook_message": ("manage_webhooks",),
    "delete_webhook_message": ("manage_webhooks",),
    # Expressions: stickers and soundboard
    "list_guild_stickers": (),
    "get_guild_sticker": (),
    "list_sticker_packs": (),
    "create_guild_sticker": ("manage_expressions",),
    "edit_guild_sticker": ("manage_expressions",),
    "delete_guild_sticker": ("manage_expressions",),
    "list_guild_sounds": (),
    "get_guild_sound": (),
    "list_default_sounds": (),
    "create_guild_sound": ("manage_expressions",),
    "edit_guild_sound": ("manage_expressions",),
    "delete_guild_sound": ("manage_expressions",),
    # Application emojis belong to the bot's application, not this server: administrators only.
    "list_app_emojis": (),
    "create_app_emoji": ("administrator",),
    "delete_app_emoji": ("administrator",),
}
UNKNOWN_TOOL_PERMISSIONS = ("administrator",)

# Holding any of these server-wide makes a member staff: they get tools (limited to their permissions) and
# no member rate limits.
STAFF_PERMISSIONS = (
    "administrator", "manage_guild", "manage_channels", "manage_roles", "manage_messages", "moderate_members",
    "kick_members", "ban_members", "manage_nicknames", "manage_events", "manage_threads", "manage_webhooks",
    "manage_expressions", "view_audit_log",
)


def is_staff(member: discord.Member, owner_ids: frozenset[int] = frozenset()) -> bool:
    if member.id in owner_ids or member.id == member.guild.owner_id:
        return True
    perms = member.guild_permissions
    return any(getattr(perms, p, False) for p in STAFF_PERMISSIONS)

# Tools whose target member must be below the requester in the role hierarchy.
MEMBER_TARGET_TOOLS = {
    "kick_member", "ban_member", "timeout_member", "remove_timeout", "set_nickname",
    "assign_role", "remove_role", "move_member", "disconnect_member", "modify_voice_state",
    "upsert_member_channel_permissions", "edit_member", "bulk_ban",
}
# Tools whose target role must be below the requester's top role.
ROLE_TARGET_TOOLS = {
    "edit_role", "delete_role", "assign_role", "remove_role", "upsert_role_channel_permissions",
    "edit_member", "create_role_menu", "reorder_roles", "prune_members",
}
# Tools that grant permissions: the requester can't hand out permissions they don't hold themselves.
# Maps tool -> argument names carrying the permissions (a numeric bitfield or comma-separated names).
GRANTING_TOOLS = {
    "create_role": ("permissions",),
    "edit_role": ("permissions",),
    "upsert_role_channel_permissions": ("allowRaw", "allowPermissions"),
    "upsert_member_channel_permissions": ("allowRaw", "allowPermissions"),
}
# Guild-wide reads that could expose channels the requester can't see: a channel must be given.
CHANNEL_REQUIRED_TOOLS = {"search_messages"}
# Tools that post content; mass pings are gated behind mention_everyone.
POSTING_TOOLS = {
    "send_message", "edit_message", "send_webhook_message", "send_private_message",
    "edit_private_message", "create_forum_post", "edit_webhook_message", "create_role_menu",
}

# Every interactive guild permission worth telling the model about.
SUMMARY_PERMISSIONS = (
    "administrator", "manage_guild", "manage_channels", "manage_roles", "manage_messages",
    "manage_webhooks", "manage_events", "manage_threads", "manage_nicknames", "manage_expressions",
    "kick_members", "ban_members", "moderate_members", "move_members", "mute_members",
    "mention_everyone", "create_instant_invite", "view_audit_log",
)


def _as_ids(value: Any) -> list[int]:
    """One ID, a comma-separated string of IDs, or a list of them."""
    parts = value if isinstance(value, list) else str(value).split(",")
    ids = []
    for part in parts:
        try:
            ids.append(int(str(part).strip()))
        except (TypeError, ValueError):
            pass
    return ids


def _find_ids(args: dict[str, Any], *needles: str) -> list[int]:
    ids = []
    for key, value in args.items():
        k = key.lower()
        if not (k.endswith("id") or k.endswith("ids")) or not any(n in k for n in needles):
            continue
        ids.extend(_as_ids(value))
    return ids


def _json_entries(value: Any) -> list[dict]:
    """A structured argument sent as a JSON string (or already a list): its dict entries."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def _role_ids(tool: str, args: dict[str, Any]) -> list[tuple[int, int | None]]:
    """(role id, requested new position or None) for every role the call touches."""
    found: list[tuple[int, int | None]] = [(rid, None) for rid in _find_ids(args, "role")]
    for key, id_keys in (("roles", ("roleId", "id")), ("positionsJson", ("id", "roleId"))):
        for entry in _json_entries(args.get(key)):
            for id_key in id_keys:
                ids = _as_ids(entry.get(id_key)) if id_key in entry else []
                if ids:
                    pos = entry.get("position")
                    found.append((ids[0], pos if isinstance(pos, int) else None))
                    break
    return found


def _granted_permissions(raw: Any) -> discord.Permissions | None:
    """Parse a numeric bitfield or 'ViewChannel, SendMessages' style names. None if unparseable."""
    text = str(raw).strip()
    if not text:
        return discord.Permissions.none()
    if text.isdigit():
        return discord.Permissions(int(text))
    perms = discord.Permissions.none()
    for name in (n for n in text.split(",") if n.strip()):
        flag = re.sub(r"(?<!^)(?=[A-Z])", "_", name.strip()).lower()  # ManageRoles -> manage_roles
        if flag not in discord.Permissions.VALID_FLAGS:
            return None
        setattr(perms, flag, True)
    return perms


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


def effective_permissions(member: discord.Member) -> discord.Permissions:
    """Everything the member can do somewhere in the server: their server-wide permissions plus any a
    channel grants them. Decides which tools the model is offered at all; each call is still checked
    against the exact channel by check_tool_permission."""
    perms = discord.Permissions(member.guild_permissions.value)
    for channel in member.guild.channels:
        perms |= channel.permissions_for(member)
    return perms


def tool_allowed(tool: str, perms: discord.Permissions, required: tuple[str, ...] | None = None) -> bool:
    """Whether a member with `perms` could make any allowed call to `tool`. Tools that fail this are left
    out of the model's tool list, so it can't call them for this member even if a message tricks it."""
    if required is None:
        required = TOOL_PERMISSIONS.get(tool, UNKNOWN_TOOL_PERMISSIONS)
        if callable(required):
            required = required({})  # the least a call can need
    return not _missing(perms, required)


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
    owner_ids: frozenset[int] = frozenset(),
    required: tuple[str, ...] | None = None,
) -> str | None:
    """Return None if allowed, otherwise a human-readable reason for the denial.

    `owner_ids` (the bot's owners) bypass every check. `required` overrides the TOOL_PERMISSIONS lookup
    (used for tools from extra MCP servers).
    """
    if author.id in owner_ids:
        return None
    guild = author.guild
    is_guild_owner = guild.owner_id == author.id
    if required is None:
        required = TOOL_PERMISSIONS.get(tool, UNKNOWN_TOOL_PERMISSIONS)
        if callable(required):
            required = required(args)

    # Channel-scoped check: channel overwrites can grant or deny per channel.
    channel_ids = _find_ids(args, "channel", "thread", "post")
    if (tool in CHANNEL_REQUIRED_TOOLS and not channel_ids and not is_guild_owner
            and not author.guild_permissions.administrator):
        return "name a channel to search (server-wide searches could expose channels you can't see)"
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
        for rid, new_position in _role_ids(tool, args):
            role = guild.get_role(rid)
            if role is not None and role >= author.top_role:
                return f"role @{role.name} is not below your highest role"
            if new_position is not None and new_position >= author.top_role.position:
                return "you can't move a role to or above your own highest role"

    for arg in GRANTING_TOOLS.get(tool, ()):
        if not _present(args, arg) or author.guild_permissions.administrator:
            continue
        granted = _granted_permissions(args[arg])
        if granted is None:
            return f"couldn't read the permissions in {arg}, so the bot won't apply them for you"
        extra = [name for name, on in granted if on and not getattr(author.guild_permissions, name, False)]
        if extra:
            return f"you can't grant permissions you don't have yourself ({', '.join(extra)})"

    if tool in POSTING_TOOLS:
        text = " ".join(_strings(args))
        if ("@everyone" in text or "@here" in text or "<@&" in text) and not author.guild_permissions.mention_everyone:
            return "you lack mention_everyone, so the bot won't post @everyone/@here/role pings for you"
        if str(args.get("allowedMentions", "")).lower() in ("all", "users_roles") and not author.guild_permissions.mention_everyone:
            return "you lack mention_everyone, so the bot can't allow role or @everyone mentions for you"

    return None
