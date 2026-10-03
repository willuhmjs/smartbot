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
        self.guild = SimpleNamespace(id=gid, owner_id=owner_id)
        self.author = author
        self.replies = []

    async def reply(self, text, **_):
        self.replies.append(text)


class FakeMember(SimpleNamespace):
    pass


def member(uid, admin=False, roles=(), manage_guild=False):
    return FakeMember(id=uid, bot=False, guild_permissions=SimpleNamespace(administrator=admin, manage_guild=manage_guild),
                      roles=[SimpleNamespace(id=r) for r in roles])


def handled(b, msg, monkeypatch):
    seen = []
    monkeypatch.setattr(b, "_is_triggered", lambda m: True)
    monkeypatch.setattr(bot.discord, "Member", FakeMember)

    async def fake_handle(m):
        seen.append(m)
    monkeypatch.setattr(b, "_handle", fake_handle)
    msg.channel = SimpleNamespace(id=1)
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
