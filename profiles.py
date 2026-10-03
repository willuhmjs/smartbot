"""Per-server profiles: one bot account with a different identity in each server.

A profiles file (PROFILES_FILE, JSON) sets the bot's nickname, avatar, banner and bio per server, plus a
persona for its system prompt and optionally who may use it there:

    {
      "default": {"persona": "You speak for Acme..."},
      "servers": {
        "123456789012345678": {"nickname": "Acme Bot", "avatar": "acme/avatar.png", "banner": "acme/banner.png",
                               "bio": "Ask me anything about Acme", "admins_only": true}
      }
    }

A server's entry is layered over "default". A field that is absent is left alone; null resets it to the
bot's global profile. Images are paths (relative to the profiles file) or https URLs. Discord rate limits
profile changes, so only fields that changed since the last apply are sent; what was sent is remembered in
PROFILE_STATE_FILE.
"""

import base64
import hashlib
import json
import logging
import mimetypes
import os
from typing import Any

import aiohttp
import discord

log = logging.getLogger("smartbot.profiles")

IDENTITY_FIELDS = ("nickname", "avatar", "banner", "bio")
FIELDS = (*IDENTITY_FIELDS, "persona", "persona_file", "admins_only", "allowed_role_ids")


class ProfileError(Exception):
    pass


class Profiles:
    def __init__(self, path: str | None, state_path: str | None = None):
        self.path = path
        self.base = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()
        self.state_path = state_path or os.path.join(self.base, ".profile-state.json")
        self.default: dict[str, Any] = {}
        self.servers: dict[int, dict[str, Any]] = {}
        self._signature: tuple | None = None
        self._state: dict[str, dict[str, Any]] = self._read_state()

    # ---------- loading ----------

    def _files(self) -> list[str]:
        files = [self.path] if self.path else []
        for entry in [self.default, *self.servers.values()]:
            if entry.get("persona_file"):
                files.append(self._resolve(entry["persona_file"]))
        return files

    def _current_signature(self) -> tuple:
        sig = []
        for f in self._files():
            try:
                sig.append((f, os.stat(f).st_mtime_ns))
            except OSError:
                sig.append((f, None))
        return tuple(sig)

    def reload_if_changed(self) -> bool:
        """Re-read the profiles file if it (or a persona file it names) changed. Returns True if it did.
        A broken file is logged and the previous profiles stay in effect."""
        if not self.path:
            return False
        if self._signature is not None and self._current_signature() == self._signature:
            return False
        try:
            default, servers = self._parse()
        except (OSError, ValueError, ProfileError) as e:
            log.error("Profiles file %s not loaded: %s", self.path, e)
            self._signature = self._current_signature()
            return False
        self.default, self.servers = default, servers
        self._signature = self._current_signature()
        log.info("Loaded profiles for %d server(s) from %s", len(servers), self.path)
        return True

    def _parse(self) -> tuple[dict, dict[int, dict]]:
        if not os.path.exists(self.path):
            return {}, {}
        with open(self.path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ProfileError("expected a JSON object")
        default = self._check_entry("default", data.get("default") or {})
        servers = {}
        for gid, entry in (data.get("servers") or {}).items():
            if not str(gid).isdigit():
                raise ProfileError(f"server keys must be server IDs, got {gid!r}")
            servers[int(gid)] = self._check_entry(gid, entry or {})
        return default, servers

    @staticmethod
    def _check_entry(name: str, entry: Any) -> dict:
        if not isinstance(entry, dict):
            raise ProfileError(f"{name}: expected an object")
        unknown = set(entry) - set(FIELDS)
        if unknown:
            raise ProfileError(f"{name}: unknown field(s) {', '.join(sorted(unknown))} (known: {', '.join(FIELDS)})")
        return entry

    def _resolve(self, path: str) -> str:
        return path if os.path.isabs(path) else os.path.join(self.base, path)

    # ---------- lookups ----------

    def for_guild(self, guild_id: int) -> dict[str, Any]:
        return {**self.default, **self.servers.get(guild_id, {})}

    def persona(self, guild_id: int) -> str:
        profile = self.for_guild(guild_id)
        if profile.get("persona_file"):
            try:
                with open(self._resolve(profile["persona_file"])) as f:
                    return f.read().strip()
            except OSError as e:
                log.warning("Persona file for server %s unreadable: %s", guild_id, e)
        return (profile.get("persona") or "").strip()

    def access(self, guild_id: int, admins_only: bool, allowed_role_ids: frozenset[int]) -> tuple[bool, frozenset[int]]:
        """Who may use the bot in this server: the profile's settings, falling back to the given defaults."""
        profile = self.for_guild(guild_id)
        if "admins_only" in profile:
            admins_only = bool(profile["admins_only"])
        if "allowed_role_ids" in profile:
            allowed_role_ids = frozenset(int(r) for r in profile["allowed_role_ids"] or ())
        return admins_only, allowed_role_ids

    # ---------- applying ----------

    async def apply(self, client: discord.Client, guild: discord.Guild) -> None:
        """Bring the bot's identity in `guild` in line with its profile, sending only what changed."""
        profile = self.for_guild(guild.id)
        state = dict(self._state.get(str(guild.id), {}))
        payload: dict[str, Any] = {}
        try:
            if "nickname" in profile and (profile["nickname"] or None) != guild.me.nick:
                payload["nick"] = profile["nickname"] or None
            for field in ("avatar", "banner"):
                if field not in profile:
                    continue
                data = await self._image(profile[field]) if profile[field] else None
                key = hashlib.sha256(data.encode()).hexdigest() if data else None
                if state.get(field, "unset") != key:
                    payload[field] = data
                    state[field] = key
            if "bio" in profile and state.get("bio", "unset") != (profile["bio"] or None):
                payload["bio"] = profile["bio"] or None
                state["bio"] = payload["bio"]
        except ProfileError as e:
            log.error("Profile for %s (%s) not applied: %s", guild.name, guild.id, e)
            return
        if not payload:
            return

        route = discord.http.Route("PATCH", "/guilds/{guild_id}/members/@me", guild_id=guild.id)
        try:
            await client.http.request(route, json=payload, reason="smartbot server profile")
        except discord.HTTPException as e:
            log.error("Couldn't update my profile in %s (%s): %s", guild.name, guild.id, e)
            return
        self._state[str(guild.id)] = state
        self._write_state()
        log.info("Updated my profile in %s (%s): %s", guild.name, guild.id, ", ".join(sorted(payload)))

    async def _image(self, ref: str) -> str:
        """An image path or URL as the data URI Discord expects."""
        if ref.startswith(("https://", "http://")):
            async with aiohttp.ClientSession() as session:
                async with session.get(ref, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        raise ProfileError(f"{ref} returned HTTP {resp.status}")
                    raw, mime = await resp.read(), resp.content_type
        else:
            path = self._resolve(ref)
            try:
                with open(path, "rb") as f:
                    raw = f.read()
            except OSError as e:
                raise ProfileError(f"can't read {path}: {e}") from e
            mime = mimetypes.guess_type(path)[0] or ""
        if mime not in ("image/png", "image/jpeg", "image/gif"):
            raise ProfileError(f"{ref} isn't a PNG, JPEG or GIF image (what Discord accepts)")
        return f"data:{mime};base64,{base64.b64encode(raw).decode()}"

    def _read_state(self) -> dict:
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _write_state(self) -> None:
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._state, f, indent=1)
            os.replace(tmp, self.state_path)
        except OSError as e:
            log.warning("Couldn't save profile state to %s: %s", self.state_path, e)
