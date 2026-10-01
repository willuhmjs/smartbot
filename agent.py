"""Tool-calling loop for Qwen (or any OpenAI-compatible chat model)."""

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

log = logging.getLogger("smartbot.agent")

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[str]]

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_TEXT_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


@dataclass
class AgentResult:
    text: str
    tools_used: list[str] = field(default_factory=list)


def mcp_tools_to_openai(tools: dict[str, dict[str, Any]], hidden_params: set[str]) -> list[dict]:
    """Convert MCP tool definitions to OpenAI function-calling format.

    Parameters in `hidden_params` (like guildId) are removed: the bot fills them in
    itself, which saves tokens and stops the model from targeting other servers.
    """
    out = []
    for name, tool in tools.items():
        schema = json.loads(json.dumps(tool["inputSchema"] or {"type": "object", "properties": {}}))
        schema.pop("$schema", None)
        props = schema.get("properties", {})
        for p in hidden_params:
            props.pop(p, None)
        if "required" in schema:
            schema["required"] = [r for r in schema["required"] if r not in hidden_params]
            if not schema["required"]:
                schema.pop("required")
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        out.append({
            "type": "function",
            "function": {"name": name, "description": tool["description"][:1024], "parameters": schema},
        })
    return out


def clean_reply(text: str | None) -> str:
    if not text:
        return ""
    text = _THINK_RE.sub("", text)
    if "</think>" in text:  # some servers drop the opening tag
        text = text.split("</think>")[-1]
    text = _TEXT_TOOL_CALL_RE.sub("", text)
    return text.strip()


def _parse_text_tool_calls(content: str) -> list[dict]:
    """Qwen sometimes emits tool calls as <tool_call>{json}</tool_call> text when the
    server's tool parser isn't enabled. Recover them instead of showing raw JSON."""
    calls = []
    for raw in _TEXT_TOOL_CALL_RE.findall(_THINK_RE.sub("", content)):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if "name" not in data:
            continue
        args = data.get("arguments", data.get("parameters", {}))
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": data["name"], "arguments": args if isinstance(args, str) else json.dumps(args)},
        })
    return calls


class Agent:
    def __init__(self, *, base_url: str, api_key: str, model: str, temperature: float,
                 extra_body: dict, max_rounds: int, tool_result_max_chars: int):
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=180)
        self.model = model
        self.temperature = temperature
        self.extra_body = extra_body
        self.max_rounds = max_rounds
        self.tool_result_max_chars = tool_result_max_chars

    async def _complete(self, messages: list[dict], tools: list[dict] | None, tool_choice: str = "auto"):
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        for attempt in range(3):
            try:
                resp = await self.client.chat.completions.create(**kwargs)
                return resp.choices[0].message
            except (APIConnectionError, APIStatusError) as e:
                status = getattr(e, "status_code", None)
                if attempt == 2 or (status is not None and status < 500 and status != 429):
                    raise
                await asyncio.sleep(2 ** attempt)

    async def run(self, messages: list[dict], tools: list[dict], execute: ToolExecutor) -> AgentResult:
        used: list[str] = []
        for _ in range(self.max_rounds):
            msg = await self._complete(messages, tools)
            content = msg.content or ""

            tool_calls = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                for tc in (msg.tool_calls or [])
            ] or _parse_text_tool_calls(content)

            if not tool_calls:
                return AgentResult(clean_reply(content), used)

            messages.append({"role": "assistant", "content": clean_reply(content), "tool_calls": tool_calls})

            # Run sequentially: later calls often depend on earlier ones (create then edit, etc.)
            for call in tool_calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be a JSON object")
                except (json.JSONDecodeError, ValueError) as e:
                    result = f"ERROR: invalid JSON arguments ({e}). Fix the arguments and try again."
                else:
                    used.append(name)
                    result = await execute(name, args)
                if len(result) > self.tool_result_max_chars:
                    result = result[: self.tool_result_max_chars] + "\n...[truncated]"
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

        # Out of rounds: force a final answer summarising what happened.
        messages.append({
            "role": "user",
            "content": "You've hit the tool-call limit. Stop calling tools and reply to the user now with "
                       "what you did, what's still left, and anything that failed.",
        })
        msg = await self._complete(messages, tools, tool_choice="none")
        return AgentResult(clean_reply(msg.content), used)
