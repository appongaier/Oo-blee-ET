"""The agents' policy front for tool calls: allow-lists, argument validation, injected context, trace.

Every call that passes these checks is sent over MCP to a role-bound server (see mcp_client.py); this
module imports no tool code and holds no database connection. The MCP server enforces the same limits
again on its side, so a bug or bypass here still cannot widen an agent's powers.

Checked here, in code, not in prompts:
- an agent can only call tools on its own allow-list;
- tenant, agent id and subject user are injected, never taken from the model;
- unknown arguments are rejected, so the model cannot pass dry_run, preview_version, reason or another
  agent's id;
- there is no LLM-facing tool that performs a confirmed erasure: forget_preview maps to the MCP `forget`
  tool with dry_run fixed to true.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agents.mcp_client import McpTransport
from app.mcp_support import AGENT_IDS, AuditLog, McpToolError
from app.tenants import TENANTS

REDACTED_ARGS = {"text"}     # personal content is kept out of traces


@dataclass
class AgentSession:
    tenant_id: str
    subject_user_id: str
    subject_display_name: str = ""
    run_id: str = field(default_factory=lambda: f"run_{uuid.uuid4().hex[:8]}")
    run_session_id: str = field(default_factory=lambda: f"sess_{uuid.uuid4().hex[:10]}")
    deleted_node_ids: list[str] | None = None     # set by the orchestrator after an erasure


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: dict
    required: tuple[str, ...]
    roles: frozenset[str]
    mcp_tool: str
    build: Callable[[AgentSession, dict], dict]    # LLM arguments -> MCP arguments (adds injected context)

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": {"type": "object", "properties": self.properties,
                                 "required": list(self.required), "additionalProperties": False}}


def _str(desc: str, **extra) -> dict:
    return {"type": "string", "description": desc, **extra}


_IDS = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 50}


def _audit_args(s: AgentSession, a: dict) -> dict:
    out: dict = {"user_id": s.subject_user_id}
    if s.deleted_node_ids:
        out["deleted_node_ids"] = s.deleted_node_ids
    if s.subject_display_name:
        out["text_terms"] = [s.subject_display_name]
    return out


TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("remember", "Record one fact the user explicitly told you about themselves. Episodic only.",
             {"text": _str("The fact, in one short sentence, as the user stated it."),
              "sensitivity": _str("How sensitive the fact is.", enum=["low", "medium", "high"])},
             ("text",), frozenset({"concierge"}), "remember",
             lambda s, a: {"user_id": s.subject_user_id, "text": a["text"], "tier": "episodic",
                           "sensitivity": a.get("sensitivity", "low"), "session_id": s.run_session_id}),
    ToolSpec("mark_superseded", "Record that a newer stored fact replaces an older one about the same user. "
             "The old fact is kept as history.",
             {"old_fact_id": _str("id of the older fact"), "new_fact_id": _str("id of the newer fact")},
             ("old_fact_id", "new_fact_id"), frozenset({"concierge"}), "mark_superseded",
             lambda s, a: {"old_fact_id": a["old_fact_id"], "new_fact_id": a["new_fact_id"]}),
    ToolSpec("find_derived", "List the stored facts about the session user and every summary, inference "
             "and action derived from them, with their text and lineage paths. Use this to read memory.",
             {}, (), frozenset({"concierge", "analyst", "auditor"}), "find_derived",
             lambda s, a: {"user_id": s.subject_user_id}),
    ToolSpec("explain_belief", "Explain why a fact, summary, inference or action exists: its parents, full "
             "provenance paths, authoring agents and memory containers.",
             {"node_id": _str("id of the node to explain")}, ("node_id",),
             frozenset({"concierge", "analyst", "auditor"}), "explain_belief", lambda s, a: {"node_id": a["node_id"]}),
    ToolSpec("trace_provenance", "Trace what a node came from (upstream) and what depends on it (downstream).",
             {"node_id": _str("id of a fact, summary, inference, action, source or user")}, ("node_id",),
             frozenset({"analyst", "auditor"}), "trace_provenance", lambda s, a: {"node_id": a["node_id"]}),
    ToolSpec("summarize", "Create a summary of stored facts (or earlier summaries). You write the summary text; "
             "it must only restate what those nodes say.",
             {"fact_ids": {**_IDS, "description": "ids of the facts or summaries being summarized"},
              "text": _str("the summary text")}, ("fact_ids", "text"), frozenset({"analyst"}), "summarize",
             lambda s, a: {"fact_ids": a["fact_ids"], "text": a["text"], "session_id": s.run_session_id}),
    ToolSpec("infer", "Record an inference that follows from stored facts or summaries, with a confidence.",
             {"fact_ids": {**_IDS, "description": "ids of the facts or summaries the inference rests on"},
              "text": _str("the inference"), "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
             ("fact_ids", "text", "confidence"), frozenset({"analyst"}), "infer",
             lambda s, a: {"fact_ids": a["fact_ids"], "text": a["text"], "confidence": a["confidence"],
                           "session_id": s.run_session_id}),
    ToolSpec("act", "Propose a synthetic action based on one or more stored inferences. Nothing is executed.",
             {"inference_ids": {**_IDS, "description": "ids of the inferences the action is based on"},
              "action_type": _str("snake_case action name, e.g. send_travel_tips")},
             ("inference_ids", "action_type"), frozenset({"analyst"}), "act",
             lambda s, a: {"inference_id": a["inference_ids"], "action_type": a["action_type"],
                           "session_id": s.run_session_id}),
    ToolSpec("audit_forgotten", "Check the graph for any remaining data about the session user. Read-only.",
             {}, (), frozenset({"auditor"}), "audit_forgotten", _audit_args),
    ToolSpec("detect_leak", "Check whether data from this tenant has leaked into another tenant. Read-only; "
             "returns a remediation preview that is never executed.",
             {"other_tenant": _str("tenant id to check", enum=sorted(TENANTS))}, ("other_tenant",),
             frozenset({"auditor"}), "detect_leak",
             lambda s, a: {"tenant_a": s.tenant_id, "tenant_b": a["other_tenant"]}),
    ToolSpec("forget_preview", "Preview what erasing the session user would delete (dry run only; changes "
             "nothing). You cannot erase data.",
             {}, (), frozenset({"auditor"}), "forget", lambda s, a: {"user_id": s.subject_user_id, "dry_run": True}),
]}


def tools_for(role: str) -> list[ToolSpec]:
    return [t for t in TOOLS.values() if role in t.roles]


def _check_type(value: Any, schema: dict) -> bool:
    t = schema.get("type")
    if t == "string":
        return isinstance(value, str) and ("enum" not in schema or value in schema["enum"])
    if t == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        return ok and schema.get("minimum", value) <= value <= schema.get("maximum", value)
    if t == "array":
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return False


def validate_args(spec: ToolSpec, args: Any) -> dict:
    if not isinstance(args, dict):
        raise McpToolError("INVALID_ARGUMENTS", "arguments must be an object")
    extra = sorted(set(args) - set(spec.properties))
    if extra:
        raise McpToolError("INVALID_ARGUMENTS", f"unexpected argument(s): {extra}")
    missing = [k for k in spec.required if k not in args]
    if missing:
        raise McpToolError("INVALID_ARGUMENTS", f"missing argument(s): {missing}")
    bad = [k for k, v in args.items() if not _check_type(v, spec.properties[k])]
    if bad:
        raise McpToolError("INVALID_ARGUMENTS", f"invalid value for: {bad}")
    return args


def redact(args: Any) -> Any:
    if not isinstance(args, dict):
        return {"_invalid": True}
    return {k: (f"<{len(v)} chars>" if k in REDACTED_ARGS and isinstance(v, str) else v) for k, v in args.items()}


def mask_reason(reason: str | None, display_name: str) -> str | None:
    """The model's stated reason is free text and may repeat what the user said. Mask the subject's name
    and cap the length before it reaches a log. Other personal details can still slip through."""
    if not reason:
        return None
    if display_name:
        reason = re.sub(re.escape(display_name), "<subject>", reason, flags=re.I)
    return reason[:160]


class ToolGateway:
    """Runs tool calls for agents over MCP and keeps the log of every attempt, allowed or not."""

    def __init__(self, session: AgentSession, transport: McpTransport, audit: AuditLog):
        self.session, self.transport, self.audit = session, transport, audit
        self.log: list[dict] = []

    def call(self, role: str, tool_name: str, args: Any, reason: str | None = None) -> dict:
        """Returns {"ok", "output", "error", "entry"}. Never raises."""
        seq = len(self.log) + 1
        trace_id = f"{self.session.run_id}-{seq}"
        masked = mask_reason(reason, self.session.subject_display_name)
        entry = {"seq": seq, "trace_id": trace_id, "agent_id": AGENT_IDS[role], "tool": tool_name,
                 "args": redact(args), "reason": masked, "status": "ok", "error_code": None, "ms": 0,
                 "statements": 0, "warnings": 0}
        self.audit.event("tool_selected", trace_id=trace_id, seq=seq, agent_id=AGENT_IDS[role], tool=tool_name,
                         reason=masked, arg_keys=sorted(args) if isinstance(args, dict) else [])
        started, out, err = time.monotonic(), None, None
        try:
            spec = TOOLS.get(tool_name)
            if spec is None or role not in spec.roles:
                raise McpToolError("TOOL_NOT_ALLOWED", f"{role} may not use '{tool_name}'")
            mcp_args = spec.build(self.session, validate_args(spec, args))
            res = self.transport.get(role).call(spec.mcp_tool, mcp_args,
                                                {"trace_id": trace_id, "seq": seq, "reason": masked or ""})
            if res["ok"]:
                out = res["output"]
                entry["statements"], entry["warnings"] = len(out["cypher"]), len(out["warnings"])
            else:
                e = res["error"]
                raise McpToolError(e.get("code", "ERROR"), e.get("message", ""), e.get("details"))
        except McpToolError as exc:
            err = exc.to_dict()
            gateway_side = exc.code in ("TOOL_NOT_ALLOWED", "INVALID_ARGUMENTS")
            entry["status"] = "rejected" if gateway_side or exc.code.startswith("FORBIDDEN") \
                or exc.code in ("TOOL_NOT_EXPOSED", "UNAUTHORIZED") else "error"
            entry["error_code"] = exc.code
            if entry["status"] == "rejected" and gateway_side:
                self.audit.event("tool_rejected", trace_id=trace_id, seq=seq, agent_id=AGENT_IDS[role],
                                 tool=tool_name, error_code=exc.code, by="gateway")
        except Exception as exc:                       # a bug must not look like a result
            err = {"code": "INTERNAL_ERROR", "message": type(exc).__name__, "details": {}}
            entry.update(status="error", error_code="INTERNAL_ERROR")
        entry["ms"] = int((time.monotonic() - started) * 1000)
        self.log.append(entry)
        return {"ok": err is None, "output": out, "error": err, "entry": entry}
