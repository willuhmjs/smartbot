"""Tool calls can't reach outside the server they were asked from, whatever the model passes."""

import asyncio
import os
import sys
from types import SimpleNamespace

import discord

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scope import check_scope  # noqa: E402

HERE, THERE = 100000000000000001, 200000000000000002
CH, CH_ELSEWHERE, OLD_THREAD = 111111111111111111, 222222222222222222, 333333333333333333
ROLE, ROLE_ELSEWHERE = 444444444444444444, 555555555555555555
MEMBER, STRANGER = 666666666666666666, 777777777777777777
HOOK, HOOK_ELSEWHERE = 888888888888888888, 999999999999999999


class NotFound(discord.NotFound):
    def __init__(self):
        Exception.__init__(self, "not found")


class Client:
    async def fetch_channel(self, cid):
        guilds = {CH_ELSEWHERE: THERE, OLD_THREAD: HERE}
        if cid not in guilds:
            raise NotFound()
        return SimpleNamespace(guild=SimpleNamespace(id=guilds[cid]))

    async def fetch_webhook(self, wid):
        return SimpleNamespace(guild_id={HOOK: HERE, HOOK_ELSEWHERE: THERE}[wid])

    async def fetch_invite(self, code):
        return SimpleNamespace(guild=SimpleNamespace(id=HERE if code == "ours" else THERE))


async def fetch_member(uid):
    raise NotFound()

guild = SimpleNamespace(id=HERE, get_channel_or_thread=lambda i: object() if i == CH else None,
                        get_role=lambda i: object() if i == ROLE else None,
                        get_member=lambda i: object() if i == MEMBER else None, fetch_member=fetch_member)


def scope(tool, args, has_guild_param=False, owner=False):
    return asyncio.run(check_scope(Client(), guild, tool, args, {"guildId"}, has_guild_param, owner))


def test_channels_roles_and_members_must_be_here():
    assert scope("send_message", {"channelId": str(CH), "content": "hi"}) is None
    assert scope("get_message", {"channelId": str(OLD_THREAD), "messageId": "1"}) is None   # fetched: ours
    assert "isn't in this server" in scope("send_message", {"channelId": str(CH_ELSEWHERE), "content": "hi"})
    assert "isn't in this server" in scope("read_messages", {"channelId": f"<#{CH_ELSEWHERE}>"})
    assert scope("forward_message", {"channelId": str(CH_ELSEWHERE), "messageId": "1", "targetChannelId": str(CH)})
    assert scope("create_automod_rule", {"exemptChannelIds": [str(CH), str(CH_ELSEWHERE)]}, True)
    assert scope("edit_channel", {"channelId": "general"})                                  # names aren't IDs
    assert scope("create_role_menu", {"channelId": str(CH), "roleIds": [str(ROLE)]}) is None
    assert "role" in scope("create_role_menu", {"channelId": str(CH), "roleIds": f"{ROLE},{ROLE_ELSEWHERE}"})
    assert scope("send_private_message", {"userId": str(MEMBER), "content": "hi"}) is None
    assert "isn't a member" in scope("send_private_message", {"userId": str(STRANGER), "content": "hi"})
    assert scope("delete_private_message", {"userId": str(MEMBER), "messageId": "5"}) is None
    assert scope("ban_member", {"guildId": str(HERE), "userId": str(STRANGER)}, True) is None  # banning by ID


def test_everything_else_is_checked_or_refused():
    assert scope("delete_message", {"messageId": "5"})                    # a message with no channel to check
    assert scope("delete_webhook", {"webhookId": str(HOOK)}) is None
    assert scope("delete_webhook", {"webhookId": str(HOOK_ELSEWHERE)})
    assert scope("send_webhook_message", {"webhookUrl": f"https://discord.com/api/webhooks/{HOOK_ELSEWHERE}/tok"})
    assert scope("delete_invite", {"inviteCode": "ours"}) is None and scope("delete_invite", {"inviteCode": "theirs"})
    assert scope("delete_emoji", {"guildId": str(HERE), "emojiId": "5"}, True) is None
    assert scope("some_new_tool", {"emojiId": "5"})                        # not pinned to this server by guildId
    assert scope("some_new_tool", {"widgetId": "5"}, True)                 # unknown kind of ID: refused
    assert scope("delete_app_emoji", {"emojiId": "5"})                     # shared by every server
    assert scope("delete_app_emoji", {"emojiId": "5"}, owner=True) is None
    assert scope("list_app_emojis", {}) is None
