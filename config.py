import json
import os
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


@dataclass(frozen=True)
class Config:
    discord_token: str
    mcp_url: str

    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_temperature: float
    llm_extra_body: dict
    vision: bool

    max_tool_rounds: int
    history_limit: int
    tool_result_max_chars: int
    tool_timeout: float

    owner_ids: frozenset[int]
    allowed_role_ids: frozenset[int]
    disabled_tools: frozenset[str]
    user_cooldown: float
    show_tool_trace: bool
    members_intent: bool


def load_config() -> Config:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set (see .env.example)")

    return Config(
        discord_token=token,
        mcp_url=os.getenv("MCP_URL", "http://localhost:8085/mcp"),
        llm_base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
        llm_api_key=os.getenv("LLM_API_KEY", "ollama"),
        llm_model=os.getenv("LLM_MODEL", "qwen3:14b"),
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.6")),
        llm_extra_body=json.loads(os.getenv("LLM_EXTRA_BODY", "{}") or "{}"),
        vision=_bool("VISION", False),
        max_tool_rounds=int(os.getenv("MAX_TOOL_ROUNDS", "12")),
        history_limit=int(os.getenv("HISTORY_LIMIT", "20")),
        tool_result_max_chars=int(os.getenv("TOOL_RESULT_MAX_CHARS", "6000")),
        tool_timeout=float(os.getenv("TOOL_TIMEOUT", "60")),
        owner_ids=_ids("OWNER_IDS"),
        allowed_role_ids=_ids("ALLOWED_ROLE_IDS"),
        disabled_tools=_names("DISABLED_TOOLS"),
        user_cooldown=float(os.getenv("USER_COOLDOWN", "3")),
        show_tool_trace=_bool("SHOW_TOOL_TRACE", True),
        members_intent=_bool("MEMBERS_INTENT", False),
    )
