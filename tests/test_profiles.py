import asyncio
import base64
import json
import os
import sys
from types import SimpleNamespace

import discord
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from profiles import ProfileError, Profiles  # noqa: E402
from store import Store  # noqa: E402

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


def profiles(tmp_path, name="profiles.json"):
    return Profiles(str(tmp_path / name), Store(str(tmp_path / "data" / "smartbot.db")))


def load(tmp_path):
    p = profiles(tmp_path)
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
    assert p.access(1, "everyone", frozenset()) == ("everyone", frozenset())
    assert p.access(5, "everyone", frozenset({7})) == ("administrator", frozenset({42}))
    # Editing the persona file counts as a change.
    (tmp_path / "persona.md").write_text("Talk like a robot.")
    os.utime(tmp_path / "persona.md", ns=(0, os.stat(tmp_path / "persona.md").st_mtime_ns + 1_000_000_000))
    assert p.reload_if_changed()


def test_no_file_means_no_profiles(tmp_path):
    p = profiles(tmp_path, "missing.json")
    p.reload_if_changed()
    assert p.for_guild(1) == {} and p.persona(1) == ""


def test_overrides_layer_and_persist(tmp_path):
    write(tmp_path, {"default": {"access": "manage_guild"}, "servers": {"5": {"nickname": "Deploy"}}})
    p = load(tmp_path)
    assert p.get(5, "nickname") == "Deploy" and p.source(5, "nickname") == "deployment"
    assert p.get(5, "antispam") is False and p.source(5, "antispam") == "default"
    p.set(5, "nickname", "Live", by=7)
    assert p.get(5, "nickname") == "Live" and p.source(5, "nickname") == "set in Discord"
    assert p.get(5, "nickname", discord_changes=False) == "Deploy"
    assert oct(os.stat(tmp_path / "data" / "smartbot.db").st_mode & 0o777) == "0o600"
    again = load(tmp_path)
    assert again.get(5, "nickname") == "Live" and again.get(6, "nickname") is None
    assert again.access(6, "everyone", frozenset())[0] == "manage_guild"
    assert again.reset(5, "nickname") and again.get(5, "nickname") == "Deploy"
    assert not again.reset(5, "nickname")
    assert load(tmp_path).get(5, "nickname") == "Deploy"


def test_uploaded_images_live_in_the_database(tmp_path):
    write(tmp_path, {})
    p = load(tmp_path)
    ref = p.save_image(PNG, "image/png")
    p.set(1, "avatar", ref)
    [payload] = apply(p, FakeClient(), guild())
    assert payload["avatar"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    p.set(1, "avatar", p.save_image(PNG + b"2", "image/png"))
    assert p.store.get_image(ref) is None  # the replaced image is dropped
    with pytest.raises(ProfileError):
        p.save_image(b"GIF89a", "image/webp")


def test_old_state_file_is_imported(tmp_path):
    (tmp_path / "a.png").write_bytes(PNG)
    write(tmp_path, {"default": {"avatar": "a.png"}})
    apply(load(tmp_path), FakeClient(), guild())
    state = load(tmp_path).store.profile_state(1)
    (tmp_path / ".profile-state.json").write_text(json.dumps({"1": state}))
    (tmp_path / "data" / "smartbot.db").unlink()
    assert apply(load(tmp_path), FakeClient(), guild()) == []  # not resent after upgrading


def test_ids_from_the_file_become_ints(tmp_path):
    write(tmp_path, {"servers": {"1": {"modlog_channel": "22", "automod_channels": ["33", 44]}}})
    p = load(tmp_path)
    assert p.get(1, "modlog_channel") == 22 and p.get(1, "automod_channels") == [33, 44]
