"""Tool filtering by the requester's permissions, and who may change which settings."""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import discord
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DISCORD_TOKEN", "test")

import bot  # noqa: E402
from config import load_config  # noqa: E402
from controls import Denied  # noqa: E402
from settings import SettingError, parse  # noqa: E402

TOOLS = ["ban_member", "list_channels", "delete_message", "edit_server", "brand_new_tool"]


def make_bot(tmp_path, monkeypatch, profiles=None, **env):
    (tmp_path / "profiles.json").write_text(json.dumps(profiles or {}))
    monkeypatch.setenv("PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    b = bot.SmartBot(load_config())
    b.mcp.tools = {n: {"description": n, "inputSchema": {"type": "object", "properties": {}}} for n in TOOLS}
    b.logged = []

    async def fake_log(guild, text):
        b.logged.append(text)
    monkeypatch.setattr(b.moderator, "log", fake_log)
    return b


def member(uid=10, channel_perms=None, **perms):
    """A member with server-wide `perms`, optionally granted more in one channel."""
    channel = SimpleNamespace(permissions_for=lambda m: discord.Permissions(**(channel_perms or {})))
    guild = SimpleNamespace(id=1, owner_id=999, channels=[channel], name="g",
                            get_channel_or_thread=lambda i: None, get_role=lambda i: None)
    return SimpleNamespace(id=uid, guild=guild, guild_permissions=discord.Permissions(**perms),
                           mention=f"<@{uid}>", display_name=f"m{uid}", top_role=1)


def offered(b, m):
    return {t["function"]["name"] for t in b._tools_for(m)[0]}


def test_tools_follow_the_requesters_permissions(tmp_path, monkeypatch):
    b = make_bot(tmp_path, monkeypatch, OWNER_IDS="7")
    manager = offered(b, member(manage_guild=True))
    assert "ban_member" not in manager and "delete_message" not in manager      # can't ban or delete
    assert {"list_channels", "edit_server", "update_bot_setting", "get_bot_settings"} <= manager
    assert "add_strikes" not in manager                                           # needs moderate_members
    assert "brand_new_tool" not in manager                                        # unknown tools: admins only

    mod = offered(b, member(moderate_members=True, ban_members=True))
    assert {"ban_member", "add_strikes", "get_strikes"} <= mod and "update_bot_setting" not in mod

    # A permission granted in one channel offers the tool; the call is still checked per channel.
    assert "delete_message" in offered(b, member(channel_perms={"manage_messages": True}))
    assert {*TOOLS, "update_bot_setting", "add_strikes"} <= offered(b, member(administrator=True))
    assert {*TOOLS, "update_bot_setting"} <= offered(b, member(uid=7))           # bot owner


def test_executor_refuses_tools_it_did_not_offer(tmp_path, monkeypatch):
    b = make_bot(tmp_path, monkeypatch)
    called = []

    async def call_tool(name, args):
        called.append(name)
        return False, "ok"
    monkeypatch.setattr(b.mcp, "call_tool", call_tool)
    m = member(manage_guild=True)
    msg = SimpleNamespace(author=m, guild=m.guild, channel=SimpleNamespace(mention="#c"))
    tools, params = b._tools_for(m)
    execute = b._make_executor(msg, params, {t["function"]["name"] for t in tools})
    # e.g. a <tool_call> the model wrote as text after a prompt injection
    assert asyncio.run(execute("ban_member", {"userId": "5"})).startswith("ERROR: unknown tool")
    assert asyncio.run(execute("update_bot_setting", {"key": "escalation", "value": [{"strikes": 2, "action": "ban"}]})) \
        .startswith("PERMISSION DENIED")
    assert called == []


def test_setting_needs_the_permissions_its_value_uses(tmp_path, monkeypatch):
    b = make_bot(tmp_path, monkeypatch)
    ban_steps = [{"strikes": 3, "action": "ban"}]
    with pytest.raises(Denied):
        asyncio.run(b.controls.change(member(), "antispam", "off"))                   # no manage_guild
    with pytest.raises(Denied, match="ban_members"):
        asyncio.run(b.controls.change(member(manage_guild=True), "escalation", ban_steps))
    with pytest.raises(Denied, match="manage_messages"):
        asyncio.run(b.controls.change(member(manage_guild=True), "automod", "on"))
    assert b.profiles.get(1, "escalation") != ban_steps

    asyncio.run(b.controls.change(member(manage_guild=True, ban_members=True), "escalation", ban_steps))
    asyncio.run(b.controls.change(member(manage_guild=True), "automod_rules", "Be nice."))
    assert b.profiles.get(1, "escalation") == ban_steps and b.profiles.get(1, "automod_rules") == "Be nice."
    assert len(b.logged) == 2 and "escalation" in b.logged[0]

    # Undoing back to a value you couldn't set yourself is refused too.
    asyncio.run(b.controls.change(member(manage_guild=True, ban_members=True), "escalation", []))
    with pytest.raises(Denied):
        asyncio.run(b.controls.reset(member(manage_guild=True), "escalation"))
    assert b.profiles.get(1, "escalation") == []


def test_parse():
    assert parse("antispam", "on") is True and parse("antispam_window", "10") == 10
    assert parse("automod_channels", "<#123456789012345678>, 223456789012345678") == [123456789012345678,
                                                                                      223456789012345678]
    assert parse("escalation", '[{"strikes": 5, "action": "ban"}, {"strikes": 2, "action": "timeout"}]') == [
        {"strikes": 2, "action": "timeout", "minutes": 60}, {"strikes": 5, "action": "ban"}]
    assert parse("automod_tiers", {"high": {"action": "log", "strikes": 3}})["high"] == {"action": "log", "strikes": 3}
    assert parse("nickname", "") is None
    for key, bad in [("antispam_window", "999"), ("access", "anyone"), ("automod_tiers", '{"low": {"action": "ban"}}'),
                     ("escalation", '[{"strikes": 2, "action": "timeout", "minutes": 99999}]'), ("persona_file", "x"),
                     ("model", "nope")]:
        with pytest.raises(SettingError):
            parse(key, bad, models=["gpt-oss-120b"])


def test_rate_limits_for_members():
    from ratelimit import RateLimiter
    r = RateLimiter()
    assert all(r.check(1, 5, 4, 30, 150, now=i) == 0 for i in range(4))
    wait = r.check(1, 5, 4, 30, 150, now=10)
    assert 49 <= wait <= 51                                    # the first of the four leaves the window at 60s
    assert r.should_notify(1, 5, wait, now=10) and not r.should_notify(1, 5, wait, now=20)
    assert r.check(1, 6, 4, 30, 150, now=10) == 0             # others aren't affected
    assert r.check(1, 5, 4, 30, 150, now=61) == 0
    for i in range(30):                                        # hour limit, spread out
        r.check(2, 5, 4, 30, 150, now=i * 61)
    assert r.check(2, 5, 4, 30, 150, now=30 * 61) > 0
    assert r.check(3, 5, 0, 0, 0, now=0) == 0                 # 0 = no limit
    for uid in range(150):                                     # many accounts together
        r.check(4, uid, 4, 30, 150, now=uid)
    assert r.check(4, 999, 4, 30, 150, now=200) > 0


def test_non_staff_get_no_tools_in_staff_mode(tmp_path, monkeypatch):
    from permissions import is_staff
    b = make_bot(tmp_path, monkeypatch, {"default": {"tools": "staff"}}, OWNER_IDS="7")
    b.profiles.reload_if_changed()
    assert not is_staff(member(send_messages=True)) and is_staff(member(manage_messages=True))
    assert is_staff(member(uid=7), frozenset({7}))
    seen = {}

    async def run(messages, tools, execute, model=None):
        seen["tools"], seen["system"] = tools, messages[0]["content"]
        seen["refused"] = await execute("searxng_web_search", {"query": "x"})
        return SimpleNamespace(text="hi", tools_used=[])

    async def build(message, minimal=False):
        return [{"role": "system", "content": "minimal" if minimal else "full"}]
    monkeypatch.setattr(b.agent, "run", run)
    monkeypatch.setattr(b, "_build_messages", build)

    async def reply(message, text):
        seen["reply"] = text
    monkeypatch.setattr(b, "_send_reply", reply)

    class Typing:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    m = member(send_messages=True)
    msg = SimpleNamespace(author=m, guild=m.guild, id=1, channel=SimpleNamespace(typing=Typing, mention="#c"))
    asyncio.run(b._handle(msg, minimal=True))
    assert seen["tools"] == [] and seen["system"] == "minimal"
    assert seen["refused"].startswith("ERROR: unknown tool")


def test_chat_only_has_no_tools_commands_or_moderation(tmp_path, monkeypatch):
    b = make_bot(tmp_path, monkeypatch, CHAT_ONLY="true", OWNER_IDS="7")
    assert b.tree is None and b.extra_mcp == []
    seen = {}

    async def run(messages, tools, execute, model=None):
        seen["tools"] = tools
        seen["refused"] = await execute("list_channels", {})
        return SimpleNamespace(text="hi", tools_used=[])
    monkeypatch.setattr(b.agent, "run", run)

    async def build(message, minimal=False):
        return [{"role": "system", "content": "x"}]
    monkeypatch.setattr(b, "_build_messages", build)

    async def reply(message, text):
        seen["reply"] = text
    monkeypatch.setattr(b, "_send_reply", reply)

    class Typing:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    m = member(uid=7, administrator=True)                      # even the owner, and with no discord-mcp running
    msg = SimpleNamespace(author=m, guild=m.guild, id=1, channel=SimpleNamespace(typing=Typing, mention="#c"))
    asyncio.run(b._handle(msg))
    assert seen["tools"] == [] and seen["reply"] == "hi"
    assert seen["refused"].startswith("ERROR: unknown tool")
    text = bot.CHAT_PROMPT.format(bot_name="B", bot_id=1, now="n", guild_name="G", channel_name="c", channel_extra="",
                                  author_name="a", author_username="a", author_id=4, persona="")
    assert "no tools" in text
