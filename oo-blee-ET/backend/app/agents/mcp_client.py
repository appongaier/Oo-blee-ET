"""MCP client for the agents: spawns role-bound MCP servers (mcp/server.py) and calls tools over stdio.

This is the only path from the agents' side of the system to the graph. The agent process holds no
database connection and imports no tool code. The SDK is async, so each client runs its session in a
background thread with its own event loop and exposes a plain blocking `call`.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client

from app.config import get_settings
from app.mcp_support import ROLE_TOOLS, McpToolError

_BACKEND = Path(__file__).resolve().parents[2]
SERVER_SCRIPT = _BACKEND.parent / "mcp" / "server.py"


@dataclass
class ServerBinding:
    tenant_id: str
    subject_user_id: str
    run_id: str
    auditor_tenants: tuple[str, ...]
    graph_names: dict[str, str] | None = None
    receipt_dir: str | None = None
    log_path: str | None = None            # None -> settings; "" -> logging off


class McpToolClient:
    """One MCP server process for one role."""

    def __init__(self, role: str, binding: ServerBinding, *, timeout: float = 60.0, startup_timeout: float = 30.0):
        if role not in ROLE_TOOLS:
            raise ValueError(f"unknown role {role}")
        self.role, self.binding, self.timeout, self.startup_timeout = role, binding, timeout, startup_timeout
        self.tools: set[str] = set()
        self.tool_schemas: dict[str, dict] = {}
        self._session: ClientSession | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    # ---------- process launch ----------
    def server_env(self) -> dict[str, str]:
        """Only what the server needs. The LLM API key is never passed."""
        s, b = get_settings(), self.binding
        env = dict(get_default_environment())
        env.update(FALKORDB_HOST=s.falkordb_host, FALKORDB_PORT=str(s.falkordb_port),
                   MCP_LOG_PATH=s.mcp_log_path if b.log_path is None else b.log_path,
                   RECEIPT_DIR=s.receipt_dir if b.receipt_dir is None else b.receipt_dir)
        if s.falkordb_password:
            env["FALKORDB_PASSWORD"] = s.falkordb_password
        if b.graph_names:
            env["MCP_GRAPH_NAMES"] = json.dumps(b.graph_names)
        return env

    def _params(self) -> StdioServerParameters:
        b = self.binding
        args = [str(SERVER_SCRIPT), "--role", self.role, "--tenant", b.tenant_id, "--subject", b.subject_user_id,
                "--auditor-tenants", ",".join(b.auditor_tenants), "--run-id", b.run_id]
        return StdioServerParameters(command=sys.executable, args=args, env=self.server_env(), cwd=str(_BACKEND))

    # ---------- lifecycle ----------
    def start(self) -> "McpToolClient":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._thread_main, name=f"mcp-{self.role}", daemon=True)
        self._thread.start()
        if not self._ready.wait(self.startup_timeout) or self._error is not None:
            err = type(self._error).__name__ if self._error else "timeout"
            self.close()
            raise McpToolError("MCP_START_FAILED", f"could not start the {self.role} MCP server ({err})")
        return self

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._session_main())
        except BaseException as exc:          # server died or never started
            self._error = self._error or exc
        finally:
            self._session = None
            self._ready.set()
            self._loop.close()

    async def _session_main(self) -> None:
        # The whole session lives in one task: anyio scopes must be exited by the task that entered them.
        self._stop = asyncio.Event()
        async with stdio_client(self._params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                self.tools = {t.name for t in listed.tools}
                self.tool_schemas = {t.name: t.input_schema for t in listed.tools}
                self._session = session
                self._ready.set()
                await self._stop.wait()

    def close(self) -> None:
        loop, stop, thread = self._loop, self._stop, self._thread
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        if thread is not None:
            thread.join(10)
        self._thread = None

    # ---------- calls ----------
    def call(self, tool: str, args: dict, meta: dict | None = None) -> dict:
        """Blocking tool call. Returns {"ok": True, "output": {...}} or {"ok": False, "error": {...}}."""
        if self._session is None or self._loop is None or self._thread is None or not self._thread.is_alive():
            return {"ok": False, "error": {"code": "MCP_TRANSPORT_ERROR", "message": "the MCP server is not running",
                                           "details": {}}}
        fut = asyncio.run_coroutine_threadsafe(self._call(tool, args, meta), self._loop)
        try:
            return fut.result(self.timeout + 5)
        except Exception as exc:
            return {"ok": False, "error": {"code": "MCP_TRANSPORT_ERROR", "message": type(exc).__name__, "details": {}}}

    async def _call(self, tool: str, args: dict, meta: dict | None) -> dict:
        try:
            res = await self._session.call_tool(tool, args, read_timeout_seconds=self.timeout, meta=meta or None)
        except Exception as exc:
            return {"ok": False, "error": {"code": "MCP_TRANSPORT_ERROR", "message": type(exc).__name__, "details": {}}}
        body = res.structured_content
        if body is None:
            return {"ok": False, "error": {"code": "MCP_PROTOCOL_ERROR", "message": "the server returned no "
                                           "structured content", "details": {}}}
        if res.is_error:
            return {"ok": False, "error": body.get("error") or {"code": "MCP_ERROR", "message": "unknown", "details": {}}}
        return {"ok": True, "output": body}

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()


class McpTransport:
    """The set of MCP servers for one orchestrated run, started lazily, one process per role."""

    def __init__(self, binding: ServerBinding):
        self.binding = binding
        self._clients: dict[str, McpToolClient] = {}

    def get(self, role: str) -> McpToolClient:
        if role not in self._clients:
            self._clients[role] = McpToolClient(role, self.binding).start()
        return self._clients[role]

    def close(self) -> None:
        for c in self._clients.values():
            c.close()
        self._clients.clear()
