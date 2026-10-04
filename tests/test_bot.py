import asyncio
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DISCORD_TOKEN", "test")

import bot  # noqa: E402
from config import load_config  # noqa: E402
from permissions import check_tool_permission  # noqa: E402


def make_bot(tmp_path, monkeypatch, profiles, **env):
    (tmp_path / "profiles.json").write_text(json.dumps(profiles))
    monkeypatch.setenv("PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return bot.SmartBot(load_config())


class Msg:
    def __init__(self, gid, author, owner_id=999):
        self.guild = SimpleNamespace(id=gid, owner_id=owner_id, me=None, name="g")
        self.author = author
        self.replies = []

    async def reply(self, text, **_):
        self.replies.append(text)


class FakeMember(SimpleNamespace):
    pass


def member(uid, admin=False, roles=(), manage_guild=False):
    return FakeMember(id=uid, bot=False, guild_permissions=SimpleNamespace(administrator=admin, manage_guild=manage_guild),
                      roles=[SimpleNamespace(id=r) for r in roles])


def handled(b, msg, monkeypatch, perms=None):
    seen = []
    monkeypatch.setattr(b, "_is_triggered", lambda m: True)
    monkeypatch.setattr(bot.discord, "Member", FakeMember)

    async def fake_handle(m, minimal=False):
        seen.append(m)
    monkeypatch.setattr(b, "_handle", fake_handle)
    msg.channel = SimpleNamespace(id=1, permissions_for=lambda m: perms or bot.discord.Permissions.text())
    msg.author.guild = msg.guild
    asyncio.run(b.on_message(msg))
    return bool(seen)


def test_access_per_server(tmp_path, monkeypatch):
    b = make_bot(tmp_path, monkeypatch, {"servers": {"1": {"admins_only": True}, "2": {"allowed_role_ids": [5]}}},
                 OWNER_IDS="7", USER_COOLDOWN="0")
    assert not handled(b, Msg(1, member(10)), monkeypatch)                     # admins only there
    assert handled(b, Msg(1, member(11, admin=True)), monkeypatch)
    assert handled(b, Msg(1, member(7)), monkeypatch)                          # bot owner
    assert not handled(b, Msg(2, member(12)), monkeypatch)                     # needs role 5
    assert handled(b, Msg(2, member(13, roles=[5])), monkeypatch)
    assert handled(b, Msg(3, member(14)), monkeypatch)                         # no profile: env defaults


def test_admins_only_default(tmp_path, monkeypatch):
    b = make_bot(tmp_path, monkeypatch, {"servers": {"2": {"admins_only": False}}}, ADMINS_ONLY="true",
                 USER_COOLDOWN="0")
    assert not handled(b, Msg(1, member(10)), monkeypatch)
    assert handled(b, Msg(2, member(10)), monkeypatch)


def test_persona_in_prompt():
    text = bot.SYSTEM_PROMPT.format(
        bot_name="B", bot_id=1, now="n", guild_name="G", guild_id=2, channel_name="c", channel_id=3,
        channel_extra="", author_name="a", author_username="a", author_id=4, author_roles="none",
        author_perms="p", no_reply=bot.NO_REPLY, persona=bot.PERSONA_SECTION.format(text="Be {brand}."))
    assert "## Persona in this server\nBe {brand}." in text


def test_owner_bypasses_tool_permissions():
    author = SimpleNamespace(id=7, guild=None)
    assert asyncio.run(check_tool_permission("delete_channel", {}, author, frozenset({7}))) is None


def test_access_manage_guild(tmp_path, monkeypatch):
    b = make_bot(tmp_path, monkeypatch, {"default": {"access": "manage_guild"}}, USER_COOLDOWN="0")
    msg = Msg(1, member(10))
    assert not handled(b, msg, monkeypatch) and "Manage Server" in msg.replies[0]
    assert handled(b, Msg(1, member(11, manage_guild=True)), monkeypatch)
    assert handled(b, Msg(1, member(12, admin=True)), monkeypatch)


def test_no_model_call_where_it_cannot_reply(tmp_path, monkeypatch):
    b = make_bot(tmp_path, monkeypatch, {}, USER_COOLDOWN="0")
    no_send = bot.discord.Permissions(view_channel=True, read_message_history=True)
    assert not handled(b, Msg(1, member(10)), monkeypatch, perms=no_send)
    assert handled(b, Msg(1, member(10)), monkeypatch)


def test_replies_mode_sends_only_the_reply_chain(tmp_path, monkeypatch):
    b = make_bot(tmp_path, monkeypatch, {}, CHAT_ONLY="true", HISTORY_MODE="replies")
    me = SimpleNamespace(id=42, display_name="ChatCS")
    monkeypatch.setattr(type(b), "user", property(lambda self: me))
    guild = SimpleNamespace(id=1, name="G", me=me, self_role=None)
    by_id = {}

    def fake(mid, author, content, reply_to=None, cached=True):
        ref = None
        if reply_to:
            ref = SimpleNamespace(message_id=reply_to, channel_id=5, resolved=by_id[reply_to] if cached else None)
        m = SimpleNamespace(id=mid, author=author, content=content, reference=ref, guild=guild, channel=channel,
                            mentions=[me], role_mentions=[], channel_mentions=[], attachments=[])
        by_id[mid] = m
        return m

    async def fetch_message(mid):
        return by_id[mid]

    async def history(**_):
        raise AssertionError("replies mode mustn't read the channel")
        yield
    channel = SimpleNamespace(id=5, name="main", history=history, fetch_message=fetch_message)
    will, bob = SimpleNamespace(id=7, display_name="Will", name="will"), SimpleNamespace(id=8, display_name="Bob", name="bob")

    alone = fake(1, will, "<@42> who is william faircloth")
    msgs = asyncio.run(b._build_messages(alone))
    assert [m["role"] for m in msgs] == ["system", "user"] and "who is william faircloth" in msgs[1]["content"]

    fake(2, me, "He's a student in the Systems Group.", reply_to=1)
    fake(3, bob, "what does he do there", reply_to=2)
    fake(4, me, "He runs the CS mirror.", reply_to=3, cached=False)  # not cached: fetched
    last = fake(5, will, "cool, thanks", reply_to=4)
    msgs = asyncio.run(b._build_messages(last))
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert msgs[1]["content"] == "Will: who is william faircloth" and msgs[3]["content"] == "Bob: what does he do there"
    assert msgs[4]["content"] == "He runs the CS mirror." and "cool, thanks" in msgs[5]["content"]
    assert "replying to" not in msgs[5]["content"]
