"""Server-side runtime for the HTTP API: graph reads, MCP clients and optional LLM access.

Everything that calls a graph *tool* goes over MCP, exactly like the agents do. The one direct read is the
visualization export (graph_view.py).
"""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from typing import Callable

from app.agents.llm import LLMError, get_llm
from app.agents.mcp_client import McpToolClient, ServerBinding
from app.config import get_settings
from app.db import get_client
from app.mcp_support import McpToolError
from app.tenants import TENANTS, graph_name_for, validate_graph_name

VIEWER = "_viewer_"        # MCP server subject for node-level reads that are not tied to a user


class Runtime:
    def __init__(self, *, graph_names: dict[str, str] | None = None, receipt_dir: str | None = None,
                 log_path: str | None = None, run_id: str | None = None, max_clients: int = 8,
                 llm_factory: Callable | None = None, demo_controls: bool = True):
        self.graph_names, self.receipt_dir, self.log_path = graph_names, receipt_dir, log_path
        self.run_id = run_id or f"api_{uuid.uuid4().hex[:8]}"
        self.max_clients, self.demo_controls = max_clients, demo_controls
        self._llm_factory = llm_factory
        self._db = None
        self._clients: "OrderedDict[tuple, McpToolClient]" = OrderedDict()
        self._lock = threading.Lock()
        self._calls = 0

    # ---------- graph reads ----------
    @property
    def db(self):
        if self._db is None:
            self._db = get_client()
        return self._db

    def graph_name(self, tenant: str) -> str:
        if self.graph_names and tenant in self.graph_names:
            return validate_graph_name(self.graph_names[tenant])
        return graph_name_for(tenant)

    def seeded(self, tenant: str) -> bool:
        return self.graph_name(tenant) in self.db.list_graphs()

    def tenants(self) -> list[str]:
        return sorted(TENANTS)

    # ---------- MCP ----------
    def _client(self, role: str, tenant: str, subject: str) -> McpToolClient:
        key = (role, tenant, subject)
        with self._lock:
            c = self._clients.get(key)
            if c is not None:
                self._clients.move_to_end(key)
                return c
            while len(self._clients) >= self.max_clients:
                self._clients.popitem(last=False)[1].close()
            binding = ServerBinding(tenant, subject, self.run_id, tuple(sorted(TENANTS)), self.graph_names,
                                    self.receipt_dir, self.log_path)
            c = self._clients[key] = McpToolClient(role, binding).start()
            return c

    def call(self, role: str, tenant: str, subject: str, tool: str, args: dict, reason: str = "api request") -> dict:
        """One tool call over MCP. Returns the tool's structured output; raises McpToolError on failure."""
        with self._lock:
            self._calls += 1
            trace = f"{self.run_id}-{self._calls}"
        res = self._client(role, tenant, subject).call(tool, args, {"trace_id": trace, "seq": self._calls,
                                                                    "reason": reason})
        if not res["ok"]:
            e = res["error"]
            raise McpToolError(e.get("code", "ERROR"), e.get("message", ""), e.get("details"))
        return res["output"]

    # ---------- LLM (optional) ----------
    def llm_available(self) -> bool:
        s = get_settings()
        return self._llm_factory is not None or bool(s.gemini_api_key) or bool(s.anthropic_api_key)

    def make_llm(self):
        if self._llm_factory is not None:
            return self._llm_factory()
        try:
            return get_llm()
        except LLMError as exc:
            raise McpToolError("LLM_NOT_CONFIGURED", str(exc)) from exc

    def close(self) -> None:
        with self._lock:
            for c in self._clients.values():
                c.close()
            self._clients.clear()


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        s = get_settings()
        _runtime = Runtime(log_path=s.mcp_log_path, demo_controls=s.demo_controls)
    return _runtime


def shutdown_runtime() -> None:
    global _runtime
    if _runtime is not None:
        _runtime.close()
        _runtime = None
