"""Shared by the MCP server (mcp/server.py) and the agents' MCP client.

Deliberately imports nothing from app.tools: the agents' side of the system must be unable to
reach the graph except over MCP.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

# Which tools each server role exposes. One server process = one role, so an agent's identity is
# fixed when the process is launched by the orchestrator, never chosen by the model.
ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    "concierge": ("remember", "mark_superseded", "find_derived", "explain_belief"),
    "analyst": ("find_derived", "explain_belief", "trace_provenance", "summarize", "infer", "act"),
    "auditor": ("find_derived", "explain_belief", "trace_provenance", "audit_forgotten", "detect_leak", "forget"),
    "operator": ("find_derived", "forget"),      # deterministic orchestrator steps; never reachable by an LLM
}
ALL_TOOLS = ("remember", "summarize", "infer", "act", "trace_provenance", "find_derived", "forget",
             "audit_forgotten", "detect_leak", "explain_belief", "mark_superseded")
AGENT_IDS = {"concierge": "agent_concierge", "analyst": "agent_analyst", "auditor": "agent_auditor"}


class McpToolError(Exception):
    """A tool call failed, over MCP. Same shape as the tools' own errors: code, message, details."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.details = code, message, details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class AuthorizationClaim:
    """Who is asking, as plain data for the operator server. A claim, not authentication."""
    principal_id: str
    role: str                    # data_subject | tenant_admin | auditor
    tenant_ids: tuple[str, ...]


# ---------- audit log ----------
_DROP_KEYS = {"text", "api_key", "password", "token", "secret", "authorization", "preview_version"}


def _clean(v):
    if isinstance(v, (bool, int, float)) or v is None:
        return v
    if isinstance(v, str):
        return v[:200]
    if isinstance(v, (list, tuple)):
        return [x if isinstance(x, (bool, int, float)) else str(x)[:120] for x in v[:100]]
    return None


class AuditLog:
    """Append-only JSON Lines. Events carry ids, counts and statuses, never user text or secrets.
    Several processes append to the same file; each event is written with one O_APPEND write."""

    def __init__(self, path: str | None, run_id: str, source: str):
        self.path, self.run_id, self.source = path or None, run_id, source

    def event(self, event: str, **fields) -> None:
        if not self.path:
            return
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "run_id": self.run_id,
               "source": self.source, "pid": os.getpid(), "event": event}
        row.update({k: _clean(v) for k, v in fields.items() if k not in _DROP_KEYS})
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            pass                      # logging must never break a tool call


def read_log(path: str, run_id: str | None = None) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                if run_id is None or r.get("run_id") == run_id:
                    rows.append(r)
    return rows


def reconstruct_handoffs(path: str, run_id: str) -> list[dict]:
    """The agent handoffs of one run, in order, rebuilt from the log alone."""
    return sorted((r for r in read_log(path, run_id) if r["event"] == "handoff"), key=lambda r: r["seq"])
