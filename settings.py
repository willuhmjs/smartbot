"""Per-server settings: what each one means, its default, how to parse it, and who may change it.

Settings come from three layers, later ones winning: the built-in defaults below (some from .env), the
deployment's profiles file (PROFILES_FILE), and changes made in Discord with /settings or by asking the bot,
which are saved in the database (store.py). Changing any setting needs Manage Server; some need more,
e.g. making strikes end in a ban needs Ban Members, so nobody can configure the bot to do what they can't.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import discord

ACCESS_MODES = ("everyone", "manage_guild", "administrator")
TOOL_MODES = ("everyone", "staff")
TIERS = ("low", "medium", "high")
TIER_ACTIONS = ("none", "log", "delete_warn")
ESCALATION_ACTIONS = ("timeout", "kick", "ban")
RAID_ACTIONS = ("alert", "verification")
ESCALATION_PERMS = {"timeout": "moderate_members", "kick": "kick_members", "ban": "ban_members"}

DEFAULT_TIERS = {
    "low": {"action": "log", "strikes": 0},
    "medium": {"action": "delete_warn", "strikes": 1},
    "high": {"action": "delete_warn", "strikes": 2},
}
DEFAULT_ESCALATION = [
    {"strikes": 3, "action": "timeout", "minutes": 60},
    {"strikes": 5, "action": "timeout", "minutes": 1440},
]
DEFAULT_RULES = """1. Be respectful: no harassment, personal attacks or threats.
2. No slurs or hate speech.
3. No spam, scams or unsolicited advertising.
4. No sexual or graphic content."""


class SettingError(ValueError):
    pass


@dataclass(frozen=True)
class Setting:
    key: str
    section: str
    kind: str          # bool, int, str, text, choice, channel, channels, roles, model, image, tiers, escalation
    help: str
    default: Any = None
    choices: tuple[str, ...] = ()
    max_len: int = 0
    min: int = 0
    max: int = 0
    editable: bool = True  # False: only the profiles file can set it
    # Extra Discord permissions needed to set this value, on top of manage_guild.
    perms: Callable[[Any], tuple[str, ...]] = field(default=lambda value: ())


def _if_on(*perms: str) -> Callable[[Any], tuple[str, ...]]:
    return lambda value: perms if value else ()


def _tier_perms(value: Any) -> tuple[str, ...]:
    actions = {t.get("action") for t in (value or {}).values()} if isinstance(value, dict) else set()
    return ("manage_messages",) if "delete_warn" in actions else ()


def _escalation_perms(value: Any) -> tuple[str, ...]:
    steps = value if isinstance(value, list) else []
    return tuple(sorted({ESCALATION_PERMS[s["action"]] for s in steps if s.get("action") in ESCALATION_PERMS}))


SETTINGS: dict[str, Setting] = {s.key: s for s in [
    # Identity
    Setting("nickname", "identity", "str", "My name in this server (empty: my username)", max_len=32),
    Setting("avatar", "identity", "image", "My picture in this server: PNG, JPEG or GIF"),
    Setting("banner", "identity", "image", "My profile banner in this server: PNG, JPEG or GIF"),
    Setting("bio", "identity", "text", "My 'About me' in this server", max_len=190),
    Setting("persona", "identity", "text", "Extra instructions for how I talk and what I know in this server",
            max_len=4000),
    Setting("persona_file", "identity", "str", "A file with the persona (profiles file only)", editable=False),
    # Who can use the bot
    Setting("access", "access", "choice", "Who can @mention me: everyone, members with Manage Server, or administrators",
            choices=ACCESS_MODES),
    Setting("allowed_role_ids", "access", "roles", "If set, only members with one of these roles can use me"),
    Setting("admins_only", "access", "bool", "Old form of access=administrator (profiles file only)", editable=False),
    Setting("tools", "access", "choice",
            "Who gets my tools: everyone (each limited to their own permissions), or staff only (members with a "
            "moderation or management permission); everyone else can only chat, without web search or images",
            default="everyone", choices=TOOL_MODES),
    Setting("member_per_minute", "access", "int", "Requests a non-staff member can make per minute (0: no limit)",
            default=4, min=0, max=60),
    Setting("member_per_hour", "access", "int", "Requests a non-staff member can make per hour (0: no limit)",
            default=30, min=0, max=1000),
    Setting("member_server_per_hour", "access", "int",
            "Requests all non-staff members together can make per hour (0: no limit)", default=150, min=0, max=10000),
    # Models
    Setting("model", "models", "model", "The model that answers when I'm @mentioned"),
    Setting("automod_model", "models", "model", "The model that checks messages for automod"),
    # AI automod
    Setting("automod", "automod", "bool", "Check every message against the rules with the automod model",
            default=False, perms=_if_on("manage_messages")),
    Setting("automod_rules", "automod", "text", "The rules automod enforces", default=DEFAULT_RULES, max_len=4000),
    Setting("automod_channels", "automod", "channels", "Only check these channels (empty: all)", default=[]),
    Setting("automod_ignored_channels", "automod", "channels", "Never check these channels", default=[]),
    Setting("automod_exempt_roles", "automod", "roles",
            "Members with these roles are never checked (Manage Messages holders never are either)", default=[]),
    Setting("automod_tiers", "automod", "tiers",
            "What happens per severity (low/medium/high): action none, log or delete_warn, plus strikes added",
            default=DEFAULT_TIERS, perms=_tier_perms),
    # Anti-spam and raids (no LLM)
    Setting("antispam", "antispam", "bool", "Delete floods, repeated messages and mass mentions",
            default=False, perms=_if_on("manage_messages")),
    Setting("antispam_max_messages", "antispam", "int", "Messages allowed per window", default=6, min=2, max=50),
    Setting("antispam_window", "antispam", "int", "The window, in seconds", default=8, min=2, max=120),
    Setting("antispam_duplicates", "antispam", "int", "The same message this many times in a minute is spam",
            default=3, min=2, max=20),
    Setting("antispam_max_mentions", "antispam", "int", "More user/role mentions than this in one message is spam",
            default=6, min=1, max=50),
    Setting("antispam_strikes", "antispam", "int", "Strikes added for spamming", default=1, min=0, max=10),
    Setting("raid_joins", "antispam", "int",
            "This many joins in a minute counts as a raid (0: off; needs the Server Members intent)",
            default=0, min=0, max=500),
    Setting("raid_action", "antispam", "choice", "On a raid: alert the mod log, or also raise verification to High",
            default="alert", choices=RAID_ACTIONS),
    # Mod log
    Setting("modlog_channel", "modlog", "channel",
            "Where I post automod actions, strikes, settings changes and moderation I do when asked"),
    # Strikes
    Setting("strike_expiry_days", "strikes", "int", "Strikes expire after this many days", default=30, min=1, max=365),
    Setting("escalation", "strikes", "escalation",
            "Steps applied when a member reaches N active strikes: timeout (with minutes), kick or ban",
            default=DEFAULT_ESCALATION, perms=_escalation_perms),
]}
FIELDS = tuple(SETTINGS)
SECTIONS = tuple(dict.fromkeys(s.section for s in SETTINGS.values()))
IDENTITY_FIELDS = ("nickname", "avatar", "banner", "bio")


def check_entry(name: str, entry: Any) -> dict:
    """Validate a profiles-file entry's shape (values are trusted: the deployment wrote them)."""
    if not isinstance(entry, dict):
        raise SettingError(f"{name}: expected an object")
    unknown = set(entry) - set(FIELDS)
    if unknown:
        raise SettingError(f"{name}: unknown field(s) {', '.join(sorted(unknown))} (known: {', '.join(FIELDS)})")
    return entry


def required_permissions(key: str, value: Any) -> tuple[str, ...]:
    return ("manage_guild", *SETTINGS[key].perms(value))


def missing_permissions(member: discord.Member, key: str, value: Any, owner_ids: frozenset[int]) -> list[str]:
    """Permissions the member lacks to set key=value (none for bot owners, the server owner and admins)."""
    if member.id in owner_ids or member.id == member.guild.owner_id:
        return []
    perms = member.guild_permissions
    if perms.administrator:
        return []
    return [p for p in required_permissions(key, value) if not getattr(perms, p, False)]


def editable_keys(member: discord.Member, owner_ids: frozenset[int]) -> list[str]:
    """Keys the member could change to some value (value-dependent extras are checked when they do)."""
    return [k for k, s in SETTINGS.items() if s.editable and not missing_permissions(member, k, None, owner_ids)]


# ---------- parsing ----------

_ID_RE = re.compile(r"\d{15,21}")
_TRUE, _FALSE = ("1", "true", "yes", "on", "enable", "enabled"), ("0", "false", "no", "off", "disable", "disabled")


def _json_or(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            raise SettingError("expected JSON") from None
    return raw


def parse(key: str, raw: Any, guild: discord.Guild | None = None, models: list[str] | None = None) -> Any:
    """Turn a value from a slash command (a string) or a tool call (any JSON) into the stored value.
    Images are handled by the caller (they need downloading); this only checks other kinds."""
    s = SETTINGS.get(key)
    if s is None:
        raise SettingError(f"unknown setting {key!r}")
    if not s.editable:
        raise SettingError(f"{key} can only be set in the deployment's profiles file")
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in ("", "none", "null")):
        if s.kind in ("str", "text", "channel", "model"):
            return None
        if s.kind in ("channels", "roles", "escalation"):
            return []
    text = raw.strip() if isinstance(raw, str) else raw

    if s.kind == "bool":
        if isinstance(text, bool):
            return text
        if str(text).lower() in _TRUE:
            return True
        if str(text).lower() in _FALSE:
            return False
        raise SettingError(f"{key} is on or off")
    if s.kind == "int":
        try:
            n = int(text)
        except (TypeError, ValueError):
            raise SettingError(f"{key} is a whole number") from None
        if not s.min <= n <= s.max:
            raise SettingError(f"{key} must be between {s.min} and {s.max}")
        return n
    if s.kind in ("str", "text"):
        value = str(text)
        if s.max_len and len(value) > s.max_len:
            raise SettingError(f"{key} can be at most {s.max_len} characters (got {len(value)})")
        return value
    if s.kind == "choice":
        value = str(text).lower().replace(" ", "_")
        if value not in s.choices:
            raise SettingError(f"{key} is one of: {', '.join(s.choices)}")
        return value
    if s.kind == "model":
        value = str(text)
        if models and value not in models:
            raise SettingError(f"{value!r} isn't a model the server offers (see list_models)")
        return value
    if s.kind in ("channel", "channels", "roles"):
        ids = [int(i) for i in _ID_RE.findall(json.dumps(text) if not isinstance(text, str) else text)]
        if s.kind == "channel":
            if len(ids) != 1:
                raise SettingError(f"{key} is one channel")
        if guild is not None:
            for i in ids:
                found = guild.get_role(i) if s.kind == "roles" else guild.get_channel_or_thread(i)
                if found is None:
                    raise SettingError(f"{'role' if s.kind == 'roles' else 'channel'} {i} isn't in this server")
        return ids[0] if s.kind == "channel" else list(dict.fromkeys(ids))
    if s.kind == "tiers":
        value = _json_or(text)
        if not isinstance(value, dict) or set(value) - set(TIERS):
            raise SettingError(f"{key} is an object with keys {', '.join(TIERS)}")
        out = {}
        for tier in TIERS:
            entry = value.get(tier, DEFAULT_TIERS[tier])
            if not isinstance(entry, dict) or entry.get("action") not in TIER_ACTIONS:
                raise SettingError(f"{key}.{tier}.action is one of: {', '.join(TIER_ACTIONS)}")
            strikes = entry.get("strikes", 0)
            if not isinstance(strikes, int) or not 0 <= strikes <= 10:
                raise SettingError(f"{key}.{tier}.strikes is 0 to 10")
            out[tier] = {"action": entry["action"], "strikes": strikes}
        return out
    if s.kind == "escalation":
        value = _json_or(text)
        if not isinstance(value, list):
            raise SettingError(f"{key} is a list of steps like {json.dumps(DEFAULT_ESCALATION[0])}")
        out = []
        for step in value:
            if not isinstance(step, dict) or step.get("action") not in ESCALATION_ACTIONS:
                raise SettingError(f"each step's action is one of: {', '.join(ESCALATION_ACTIONS)}")
            n = step.get("strikes")
            if not isinstance(n, int) or not 1 <= n <= 100:
                raise SettingError("each step needs strikes: 1 to 100")
            clean = {"strikes": n, "action": step["action"]}
            if step["action"] == "timeout":
                minutes = step.get("minutes", 60)
                if not isinstance(minutes, int) or not 1 <= minutes <= 40320:  # Discord's limit is 28 days
                    raise SettingError("a timeout step needs minutes: 1 to 40320 (28 days)")
                clean["minutes"] = minutes
            out.append(clean)
        if len({st["strikes"] for st in out}) != len(out):
            raise SettingError("two steps have the same strike count")
        return sorted(out, key=lambda st: st["strikes"])
    raise SettingError(f"{key} can't be set this way")


def describe(key: str, value: Any, guild: discord.Guild | None = None) -> str:
    """A short human-readable form of a value."""
    s = SETTINGS[key]
    if value is None or value == [] or value == "":
        return "not set"
    if s.kind == "bool":
        return "on" if value else "off"
    if s.kind == "channel":
        return f"<#{value}>"
    if s.kind == "channels":
        return ", ".join(f"<#{c}>" for c in value)
    if s.kind == "roles":
        return ", ".join(f"<@&{r}>" for r in value)
    if s.kind == "image":
        return "custom image" if not str(value).startswith("http") else str(value)
    if s.kind == "tiers":
        return "; ".join(f"{t}: {v['action']}" + (f" +{v['strikes']} strike(s)" if v["strikes"] else "")
                         for t, v in value.items())
    if s.kind == "escalation":
        return "; ".join(f"{st['strikes']} strikes: {st['action']}" + (f" {st['minutes']}m" if "minutes" in st else "")
                         for st in value)
    text = str(value)
    return text if len(text) <= 300 else text[:300] + "…"
