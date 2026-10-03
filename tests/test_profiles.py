import asyncio
import json
import os
import sys
from types import SimpleNamespace

import discord

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from profiles import Profiles  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class FakeClient:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail
        self.http = self

    async def request(self, route, json=None, reason=None):
        if self.fail:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "boom")
        self.calls.append((route.method, route.url, json))


def guild(gid=1, nick=None):
    return SimpleNamespace(id=gid, name=f"g{gid}", me=SimpleNamespace(nick=nick))


def write(tmp_path, data):
    (tmp_path / "profiles.json").write_text(json.dumps(data))
    # mtime granularity can hide quick rewrites; force a distinct one.
    st = os.stat(tmp_path / "profiles.json")
    os.utime(tmp_path / "profiles.json", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def load(tmp_path):
    p = Profiles(str(tmp_path / "profiles.json"))
    assert p.reload_if_changed()
    return p


def apply(p, client, g):
    asyncio.run(p.apply(client, g))
    return [c[2] for c in client.calls]


def test_sends_only_changes(tmp_path):
    (tmp_path / "a.png").write_bytes(PNG)
    write(tmp_path, {"servers": {"1": {"nickname": "Acme", "avatar": "a.png", "bio": "hi"}}})
    p, c = load(tmp_path), FakeClient()
    [payload] = apply(p, c, guild())
    assert payload["nick"] == "Acme" and payload["bio"] == "hi"
    assert payload["avatar"].startswith("data:image/png;base64,")
    assert "banner" not in payload  # absent fields are left alone
    assert c.calls[0][1].endswith("/guilds/1/members/@me")

    c.calls.clear()
    assert apply(p, c, guild(nick="Acme")) == []  # nothing changed

    (tmp_path / "a.png").write_bytes(PNG + b"1")
    [payload] = apply(p, c, guild(nick="Acme"))
    assert list(payload) == ["avatar"]  # only the image that changed

    # Someone renamed the bot by hand: the nickname is checked against Discord, so it's put back.
    c.calls.clear()
    assert apply(p, c, guild(nick="manual")) == [{"nick": "Acme"}]


def test_state_survives_restart(tmp_path):
    (tmp_path / "a.png").write_bytes(PNG)
    write(tmp_path, {"default": {"avatar": "a.png"}})
    apply(load(tmp_path), FakeClient(), guild())
    c = FakeClient()
    assert apply(load(tmp_path), c, guild()) == []


def test_null_resets_and_layering(tmp_path):
    write(tmp_path, {"default": {"nickname": "Brand", "bio": "brand bio"},
                     "servers": {"2": {"nickname": None, "bio": None}}})
    p = load(tmp_path)
    assert apply(p, FakeClient(), guild(1)) == [{"nick": "Brand", "bio": "brand bio"}]
    # nick None == current None, so only the bio reset is sent.
    assert apply(p, FakeClient(), guild(2)) == [{"bio": None}]


def test_failed_request_is_retried(tmp_path):
    write(tmp_path, {"default": {"bio": "x"}})
    p = load(tmp_path)
    apply(p, FakeClient(fail=True), guild())
    assert apply(p, FakeClient(), guild()) == [{"bio": "x"}]


def test_bad_image_sends_nothing(tmp_path):
    (tmp_path / "a.txt").write_text("not an image")
    write(tmp_path, {"default": {"nickname": "N", "avatar": "a.txt"}})
    assert apply(load(tmp_path), FakeClient(), guild()) == []


def test_broken_file_keeps_previous(tmp_path):
    write(tmp_path, {"default": {"persona": "old"}})
    p = load(tmp_path)
    write(tmp_path, {"default": {"persona": "new", "colour": "red"}})  # unknown field
    assert not p.reload_if_changed()
    assert p.persona(1) == "old"
    (tmp_path / "profiles.json").write_text("{not json")
    os.utime(tmp_path / "profiles.json", ns=(0, os.stat(tmp_path / "profiles.json").st_mtime_ns + 2_000_000_000))
    assert not p.reload_if_changed()
    assert p.persona(1) == "old"


def test_persona_file_and_access(tmp_path):
    (tmp_path / "persona.md").write_text("Talk like a pirate.\n")
    write(tmp_path, {"default": {"persona": "Plain."},
                     "servers": {"5": {"persona_file": "persona.md", "admins_only": True,
                                       "allowed_role_ids": ["42"]}}})
    p = load(tmp_path)
    assert p.persona(1) == "Plain."
    assert p.persona(5) == "Talk like a pirate."
    assert p.access(1, False, frozenset()) == (False, frozenset())
    assert p.access(5, False, frozenset({7})) == (True, frozenset({42}))
    # Editing the persona file counts as a change.
    (tmp_path / "persona.md").write_text("Talk like a robot.")
    os.utime(tmp_path / "persona.md", ns=(0, os.stat(tmp_path / "persona.md").st_mtime_ns + 1_000_000_000))
    assert p.reload_if_changed()


def test_no_file_means_no_profiles(tmp_path):
    p = Profiles(str(tmp_path / "missing.json"))
    p.reload_if_changed()
    assert p.for_guild(1) == {} and p.persona(1) == ""
