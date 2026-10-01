"""Long-lived connection to an MCP server: discord-mcp over HTTP, or extra servers
(e.g. SearXNG search) launched as a subprocess over stdio.

The MCP SDK uses anyio cancel scopes that must be entered and exited in the same
task, so the session lives inside one dedicated background task. That task also
reconnects (or relaunches the subprocess) automatically if the server goes away.
"""

import asyncio
import json
import logging
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

try:
    from mcp.client.streamable_http import streamablehttp_client as _http_client
except ImportError:  # newer SDK naming
    from mcp.client.streamable_http import streamable_http_client as _http_client

log = logging.getLogger("smartbot.mcp")


def _field(obj: Any, snake: str, camel: str) -> Any:
    """mcp 2.x renamed fields to snake_case (inputSchema -> input_schema); accept both."""
    return getattr(obj, snake, None) if hasattr(obj, snake) else getattr(obj, camel, None)


class McpClient:
    def __init__(self, name: str, *, url: str | None = None, command: str | None = None,
                 args: list[str] | None = None, env: dict[str, str] | None = None, timeout: float = 60.0):
        if not url and not command:
            raise ValueError(f"MCP server {name!r} needs a url or a command")
        self.name = name
        self.url = url
        # Only a minimal environment (PATH, HOME, ...) plus the configured vars, so secrets like
        # DISCORD_TOKEN never reach third-party servers.
        self.stdio = None if url else StdioServerParameters(
            command=command, args=args or [], env={**get_default_environment(), **(env or {})})
        self.timeout = timeout
        self.session: ClientSession | None = None
        self.tools: dict[str, dict[str, Any]] = {}  # name -> {"description", "inputSchema"}
        self._ready = asyncio.Event()
        self._restart = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task | None = None

    async def start(self, wait: float = 60.0) -> None:
        """Connect in the background, waiting up to `wait` seconds for the first connection."""
        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.name}")
        if wait <= 0:
            return
        try:
            await asyncio.wait_for(self._ready.wait(), wait)
        except asyncio.TimeoutError:
            log.warning("MCP server %s not reachable yet; will keep retrying", self.name)

    def _transport(self):
        return _http_client(self.url) if self.url else stdio_client(self.stdio)

    async def stop(self) -> None:
        self._stopping = True
        self._restart.set()
        if self._task:
            await asyncio.wait([self._task], timeout=5)

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                async with self._transport() as streams:
                    read, write = streams[0], streams[1]
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        self.tools = {
                            t.name: {"description": t.description or "", "inputSchema": _field(t, "input_schema", "inputSchema") or {}}
                            for t in listed.tools
                        }
                        self.session = session
                        self._restart.clear()
                        self._ready.set()
                        backoff = 1.0
                        log.info("Connected to %s: %d tools", self.name, len(self.tools))
                        await self._restart.wait()
            except Exception as e:  # noqa: BLE001 - keep the connection loop alive no matter what
                log.warning("MCP %s connection error: %r", self.name, e)
            finally:
                self.session = None
                self._ready.clear()
            if not self._stopping:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        """Returns (is_error, text)."""
        for attempt in range(2):
            try:
                await asyncio.wait_for(self._ready.wait(), 15)
            except asyncio.TimeoutError:
                return True, f"The {self.name} tool server is unavailable right now."
            session = self.session
            if session is None:
                continue
            try:
                result = await asyncio.wait_for(session.call_tool(name, arguments), self.timeout)
            except asyncio.TimeoutError:
                return True, f"Tool '{name}' timed out after {self.timeout:.0f}s."
            except Exception as e:  # noqa: BLE001
                if attempt == 0:
                    log.warning("Tool call failed (%r); reconnecting and retrying once", e)
                    self._restart.set()
                    await asyncio.sleep(0.5)
                    continue
                return True, f"Tool '{name}' failed: {e}"
            return bool(_field(result, "is_error", "isError")), _content_to_text(result)
        return True, f"The {self.name} tool server is unavailable right now."


def _content_to_text(result: Any) -> str:
    parts = []
    for item in result.content or []:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(json.dumps(item.model_dump(exclude_none=True), default=str)[:500])
    structured = _field(result, "structured_content", "structuredContent")
    if not parts and structured:
        parts.append(json.dumps(structured, default=str))
    return "\n".join(parts) or "(no output)"
