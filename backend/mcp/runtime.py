"""Load mcp/servers.json, talk to vendor MCP, expose OpenAI tools= schemas.

Prefers hosted Streamable HTTP (SocialCrawl). Stdio (`npx`) remains supported but is
slower to start on Windows. Native tools stay in backend.tools; MCP tools merge
into the same function-calling list so the model fills arguments via tool_choice=auto.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from ..config import REPO_ROOT, env

_SERVERS_PATH = Path(__file__).resolve().parent / "servers.json"
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: str) -> str:
    def repl(m: re.Match[str]) -> str:
        return env(m.group(1)) or os.environ.get(m.group(1), "")

    return _ENV_REF.sub(repl, value)


def _expand_env_map(raw: dict[str, Any] | list[Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            out[str(k)] = _expand(str(v)) if v is not None else env(str(k))
    elif isinstance(raw, list):
        for k in raw:
            name = str(k)
            out[name] = env(name)
    return {k: v for k, v in out.items() if v}


def _expand_headers(raw: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): _expand(str(v)) for k, v in raw.items() if v is not None and _expand(str(v))}


def load_server_configs(path: Path = _SERVERS_PATH) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or {}
    if not isinstance(servers, dict):
        return {}
    return {str(k): v for k, v in servers.items() if isinstance(v, dict)}


def mcp_tool_to_openai_schema(tool: Any, *, prefix: str = "") -> dict[str, Any]:
    name = str(tool.name)
    if prefix and not name.startswith(prefix):
        name = f"{prefix}{name}"
    params = tool.inputSchema if isinstance(getattr(tool, "inputSchema", None), dict) else {
        "type": "object", "properties": {},
    }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": (tool.description or name).strip(),
            "parameters": params,
        },
    }


def _content_to_text(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
        else:
            parts.append(str(block))
    if not parts and getattr(result, "structuredContent", None) is not None:
        return json.dumps(result.structuredContent, ensure_ascii=False)
    return "\n".join(parts) if parts else json.dumps({"ok": True}, ensure_ascii=False)


def mcp_enabled() -> bool:
    """SocialCrawl only when a key is present.

    Under pytest the server is skipped unless KA_ACCOUNT_MCP=1 — otherwise every
    empty-plan turn would open a network MCP session in CI.
    """
    if not env("SOCIALCRAWL_API_KEY"):
        return False
    if os.environ.get("PYTEST_CURRENT_TEST") and env("KA_ACCOUNT_MCP") != "1":
        return False
    return True


class _McpServer:
    """One MCP connection (HTTP or stdio) + listed tools."""

    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        self.cfg = cfg
        self._transport_cm: Any = None
        self._session_cm: Any = None
        self._http: httpx.AsyncClient | None = None
        self._session: ClientSession | None = None
        self._schemas: list[dict[str, Any]] = []
        self._tool_names: set[str] = set()

    async def start(self) -> None:
        if self._session is not None:
            return
        transport = str(self.cfg.get("transport") or "stdio").lower()
        if transport in ("http", "streamable_http", "sse"):
            await self._start_http()
        else:
            await self._start_stdio()
        assert self._session is not None
        await self._session.initialize()
        listed = await self._session.list_tools()
        # Client validates structuredContent against each tool's outputSchema. SocialCrawl
        # often returns extra keys (e.g. warnings); keep the cache keys but drop schemas
        # so validate_tool_result becomes a no-op without re-fetching list_tools.
        cached = getattr(self._session, "_tool_output_schemas", None)
        if isinstance(cached, dict):
            for key in list(cached):
                cached[key] = None
        for tool in listed.tools:
            schema = mcp_tool_to_openai_schema(tool)
            self._schemas.append(schema)
            self._tool_names.add(schema["function"]["name"])

    async def _start_http(self) -> None:
        url = _expand(str(self.cfg.get("url") or ""))
        if not url:
            raise RuntimeError(f"MCP server {self.name!r} has no url")
        headers = _expand_headers(self.cfg.get("headers"))
        self._http = httpx.AsyncClient(headers=headers, timeout=60.0)
        self._transport_cm = streamable_http_client(url, http_client=self._http)
        streams = await self._transport_cm.__aenter__()
        # TransportStreams is a tuple-like (read, write, …)
        read, write = streams[0], streams[1]
        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()

    async def _start_stdio(self) -> None:
        command = _expand(str(self.cfg.get("command") or ""))
        args = [_expand(str(a)) for a in (self.cfg.get("args") or [])]
        if not command:
            raise RuntimeError(f"MCP server {self.name!r} has no command")
        params = StdioServerParameters(
            command=command,
            args=args,
            env={**os.environ, **_expand_env_map(self.cfg.get("env"))} or None,
            cwd=str(REPO_ROOT),
        )
        self._transport_cm = stdio_client(params)
        read, write = await self._transport_cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await self.start()
        assert self._session is not None
        try:
            result = await self._session.call_tool(tool_name, arguments or {})
            text = _content_to_text(result)
            if getattr(result, "isError", False):
                return {"ok": False, "data": None, "error": text}
            try:
                data: Any = json.loads(text)
            except json.JSONDecodeError:
                data = {"text": text}
            return {"ok": True, "data": data, "error": None}
        except Exception as err:
            return {"ok": False, "data": None, "error": str(err)}

    async def close(self) -> None:
        if self._session_cm is not None:
            await self._session_cm.__aexit__(None, None, None)
            self._session_cm = None
            self._session = None
        if self._transport_cm is not None:
            await self._transport_cm.__aexit__(None, None, None)
            self._transport_cm = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self._schemas.clear()
        self._tool_names.clear()


class McpHub:
    """Process-wide MCP tool registry. Lazy-connects on first use.

    MCP sessions are tied to one event loop, so we keep a daemon thread with a
    long-lived loop and schedule work onto it (plain asyncio.run per call would
    close the connection after list_tools).
    """

    def __init__(self, configs: dict[str, dict[str, Any]] | None = None, *, start_timeout: float = 30.0):
        self._configs = configs if configs is not None else load_server_configs()
        self._servers: dict[str, _McpServer] = {}
        self._by_tool: dict[str, _McpServer] = {}
        self._ready = False
        self._failed: str | None = None
        self._start_timeout = start_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._loop.is_running():
            return self._loop
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run_loop() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=_run_loop, name="ka_account-mcp", daemon=True)
        thread.start()
        ready.wait(timeout=5)
        self._loop = loop
        self._thread = thread
        return loop

    def _run(self, coro: Any, *, timeout: float | None = None) -> Any:
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout if timeout is not None else max(60.0, self._start_timeout + 30))

    async def _ensure_async(self) -> None:
        if self._ready:
            return
        if self._failed:
            raise RuntimeError(self._failed)
        if not mcp_enabled():
            self._ready = True
            return
        for name, cfg in self._configs.items():
            transport = str(cfg.get("transport") or "stdio").lower()
            if transport in ("http", "streamable_http", "sse"):
                if not cfg.get("url"):
                    continue
            elif not cfg.get("command"):
                continue
            server = _McpServer(name, cfg)
            try:
                await asyncio.wait_for(server.start(), timeout=self._start_timeout)
            except Exception as err:
                self._failed = f"MCP {name!r} failed to start: {err}"
                await server.close()
                raise RuntimeError(self._failed) from err
            self._servers[name] = server
            for tool_name in server._tool_names:
                self._by_tool[tool_name] = server
        self._ready = True

    def ensure(self) -> None:
        if not self._ready and self._failed is None:
            self._run(self._ensure_async(), timeout=self._start_timeout + 15)

    @property
    def schemas(self) -> list[dict[str, Any]]:
        try:
            self.ensure()
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for server in self._servers.values():
            out.extend(server._schemas)
        return out

    def has_tool(self, name: str) -> bool:
        try:
            self.ensure()
        except Exception:
            return False
        return name in self._by_tool

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.ensure()
        server = self._by_tool.get(name)
        if server is None:
            return {"ok": False, "data": None, "error": f"unknown MCP tool {name}"}
        return self._run(server.call(name, arguments))

    def close(self) -> None:
        async def _close_all() -> None:
            for server in list(self._servers.values()):
                try:
                    await server.close()
                except Exception:
                    pass
            self._servers.clear()
            self._by_tool.clear()
            self._ready = False
            self._failed = None

        if self._loop is not None and self._loop.is_running():
            try:
                self._run(_close_all(), timeout=15)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._loop = None
            self._thread = None


_hub: McpHub | None = None


def get_hub() -> McpHub:
    global _hub
    if _hub is None:
        _hub = McpHub()
    return _hub


def reset_hub() -> None:
    global _hub
    if _hub is not None:
        _hub.close()
        _hub = None


def make_mcp_tool_fn(hub: McpHub, tool_name: str) -> Callable[..., dict[str, Any]]:
    def run(arguments: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return hub.call(tool_name, arguments if isinstance(arguments, dict) else {})

    return run
