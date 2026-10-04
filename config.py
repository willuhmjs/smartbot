import json
import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _ids(name: str) -> frozenset[int]:
    raw = os.getenv(name, "")
    return frozenset(int(x) for x in raw.replace(" ", "").split(",") if x)


def _names(name: str) -> frozenset[str]:
    raw = os.getenv(name, "")
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def load_mcp_servers(path: str) -> tuple[list[dict], list[str]]:
    """Read extra MCP servers from a Claude-style {"mcpServers": {...}} JSON file.

    "${VAR}" in args/env/url is replaced from the environment (.env). A server that references an
    unset or empty variable is skipped, so optional servers can ship in the file but stay off until
    configured. Returns (servers, skip messages).
    """
    if not os.path.exists(path):
        return [], []
    with open(path) as f:
        entries = json.load(f).get("mcpServers", {})

    servers, skipped = [], []
    for name, entry in entries.items():
        missing: list[str] = []

        def expand(value: str) -> str:
            def sub(m: re.Match) -> str:
                val = os.getenv(m.group(1), "")
                if not val:
                    missing.append(m.group(1))
                return val
            return re.sub(r"\$\{(\w+)\}", sub, value)

        server = {
            "name": name,
            "url": expand(entry["url"]) if entry.get("url") else None,
            "command": entry.get("command"),
            "args": [expand(a) for a in entry.get("args", [])],
            "env": {k: expand(v) for k, v in entry.get("env", {}).items()},
            # Discord permissions needed to use this server's tools; omitted means administrator.
            "permissions": tuple(entry.get("permissions", ("administrator",))),
        }
        if missing:
            skipped.append(f"{name} (set {', '.join(sorted(set(missing)))} to enable)")
        else:
            servers.append(server)
    return servers, skipped


@dataclass(frozen=True)
class Config:
    discord_token: str
    chat_only: bool  # no tools at all (no discord-mcp, extra MCP servers or settings tools), no slash commands or moderation
    mcp_url: str
    mcp_socket: str  # Unix socket path for discord-mcp; empty means connect to mcp_url over TCP

    llm_base_url: str
    llm_api_key: str
    llm_model: str
    automod_model: str
    llm_temperature: float
    llm_extra_body: dict
    vision: bool

    max_tool_rounds: int
    history_limit: int
    tool_result_max_chars: int
    tool_timeout: float

    owner_ids: frozenset[int]
    access: str  # who can @mention the bot by default: everyone, manage_guild or administrator
    allowed_role_ids: frozenset[int]
    disabled_tools: frozenset[str]
    user_cooldown: float
    show_tool_trace: bool
    members_intent: bool

    mcp_servers: tuple[dict, ...]
    mcp_servers_skipped: tuple[str, ...]

    profiles_file: str | None
    profile_state_file: str | None  # older versions' state file, imported once
    database_file: str


def load_config() -> Config:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set (see .env.example)")
    # Relative paths are relative to the bot's directory, not the working directory.
    here = os.path.dirname(os.path.abspath(__file__))
    servers_file = os.path.join(here, os.getenv("MCP_SERVERS_FILE") or "mcp_servers.json")
    servers, skipped = load_mcp_servers(servers_file)
    data_dir = os.path.join(here, os.getenv("DATA_DIR") or "data")
    access = (os.getenv("ACCESS") or ("administrator" if _bool("ADMINS_ONLY", False) else "everyone")).strip().lower()
    if access not in ("everyone", "manage_guild", "administrator"):
        raise SystemExit(f"ACCESS must be everyone, manage_guild or administrator (got {access!r})")

    return Config(
        discord_token=token,
        chat_only=_bool("CHAT_ONLY", False),
        mcp_url=os.getenv("MCP_URL", "http://localhost:8085/mcp"),
        mcp_socket=os.getenv("MCP_SOCKET", ""),
        llm_base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
        llm_api_key=os.getenv("LLM_API_KEY", "ollama"),
        llm_model=os.getenv("LLM_MODEL", "qwen3:14b"),
        automod_model=os.getenv("AUTOMOD_MODEL") or os.getenv("LLM_MODEL", "qwen3:14b"),
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.6")),
        llm_extra_body=json.loads(os.getenv("LLM_EXTRA_BODY", "{}") or "{}"),
        vision=_bool("VISION", False),
        max_tool_rounds=int(os.getenv("MAX_TOOL_ROUNDS", "12")),
        history_limit=int(os.getenv("HISTORY_LIMIT", "20")),
        tool_result_max_chars=int(os.getenv("TOOL_RESULT_MAX_CHARS", "6000")),
        tool_timeout=float(os.getenv("TOOL_TIMEOUT", "60")),
        owner_ids=_ids("OWNER_IDS"),
        access=access,
        allowed_role_ids=_ids("ALLOWED_ROLE_IDS"),
        disabled_tools=_names("DISABLED_TOOLS"),
        user_cooldown=float(os.getenv("USER_COOLDOWN", "3")),
        show_tool_trace=_bool("SHOW_TOOL_TRACE", True),
        members_intent=_bool("MEMBERS_INTENT", False),
        mcp_servers=tuple(servers),
        mcp_servers_skipped=tuple(skipped),
        profiles_file=os.path.join(here, os.getenv("PROFILES_FILE") or "profiles.json"),
        profile_state_file=os.getenv("PROFILE_STATE_FILE") or None,
        database_file=os.path.join(here, os.getenv("DATABASE_FILE") or os.path.join(data_dir, "smartbot.db")),
    )
