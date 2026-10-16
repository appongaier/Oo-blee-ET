"""MCP server for The Forgetting Agent's graph tools (official MCP Python SDK, stdio transport).

One process = one role + one tenant + one subject user, fixed at launch by the orchestrator:

    python mcp/server.py --role auditor --tenant acme --subject user_ravi --run-id run_x

- It exposes only that role's tools (`ROLE_TOOLS`); anything else is "not exposed".
- Tenant, agent identity and subject user come from the launch binding, not from tool arguments.
- Arguments are validated against each tool's published schema, and unknown arguments are rejected.
- Every call is written to the JSON Lines audit log (ids, status, timing, the caller's stated reason).
- Results are structured: {"result", "cypher", "params", "graph", "warnings"} or {"error": {...}}.

Do not add an __init__.py to this directory: a top-level `mcp` package would shadow the SDK.
Do not print to stdout: it is the protocol channel.
"""
import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pydantic import BaseModel, ConfigDict, Field, ValidationError  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.types import CallToolResult, TextContent  # noqa: E402

from app.db import get_client  # noqa: E402
from app.mcp_support import AGENT_IDS, ALL_TOOLS, ROLE_TOOLS, AuditLog  # noqa: E402
from app.tools import (Authorization, ToolContext, ToolError, act, audit_forgotten, detect_leak,  # noqa: E402
                       explain_belief, find_derived, forget, infer, mark_superseded, remember, summarize,
                       trace_provenance)

IdStr = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:\-]{1,120}$")]
IdList = Annotated[list[IdStr], Field(min_length=1, max_length=50)]
Text = Annotated[str, Field(min_length=1, max_length=1000)]
Session = Annotated[str | None, Field(pattern=r"^[a-z0-9_]{3,40}$")]
Tier = Literal["episodic", "semantic", "procedural"]
Sensitivity = Literal["low", "medium", "high"]


class AuthorizationClaim(BaseModel):
    """Who is asking. A claim supplied by the (trusted) operator client; there is no authentication."""
    model_config = ConfigDict(extra="forbid")
    principal_id: IdStr
    role: Literal["data_subject", "tenant_admin", "auditor"]
    tenant_ids: Annotated[list[IdStr], Field(min_length=1, max_length=5)]


@dataclass
class Binding:
    role: str
    tenant: str
    subject: str
    allowed_tenants: tuple[str, ...]
    graph_names: dict[str, str] | None
    receipt_dir: str | None
    run_id: str
    log: AuditLog


B: Binding            # set in main()
_client = None


def _ctx() -> ToolContext:
    global _client
    if _client is None:
        _client = get_client()
    return ToolContext(_client, B.tenant, B.graph_names, B.receipt_dir)


def _agent() -> str:
    return AGENT_IDS[B.role]


def _subject(user_id: str) -> None:
    if user_id != B.subject:
        raise ToolError("FORBIDDEN_SUBJECT", "this server is bound to a different subject user")


def _auditor_auth() -> Authorization:
    return Authorization(AGENT_IDS["auditor"], "auditor", B.allowed_tenants)


def _body(body: dict, is_error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(body, default=str))],
                          structured_content=body, is_error=is_error)


def _exec(fn: Callable[[], dict]) -> CallToolResult:
    try:
        return _body(fn())
    except ToolError as exc:
        return _body({"error": exc.to_dict()}, True)
    except Exception as exc:                          # a bug must not look like a result, or leak internals
        return _body({"error": {"code": "INTERNAL_ERROR", "message": type(exc).__name__, "details": {}}}, True)


# ---------- the eleven tools ----------
def t_remember(user_id: IdStr, text: Text, tier: Tier, sensitivity: Sensitivity = "low",
               session_id: Session = None) -> CallToolResult:
    """Record a fact about a user with its source, memory container and provenance edges."""
    def run():
        _subject(user_id)
        if B.role == "concierge" and tier != "episodic":
            raise ToolError("FORBIDDEN_POLICY", "the Concierge writes episodic memories only")
        return remember(_ctx(), user_id, text, tier, _agent(), sensitivity=sensitivity, session_id=session_id)
    return _exec(run)


def t_summarize(fact_ids: IdList, text: Text, session_id: Session = None) -> CallToolResult:
    """Create a summary of facts or earlier summaries (SUMMARIZES edges to every source)."""
    return _exec(lambda: summarize(_ctx(), _agent(), fact_ids, text, session_id=session_id))


def t_infer(fact_ids: IdList, text: Text, confidence: Annotated[float, Field(ge=0, le=1)],
            session_id: Session = None) -> CallToolResult:
    """Create an inference with INFERRED_FROM edges to every source fact or summary."""
    return _exec(lambda: infer(_ctx(), _agent(), fact_ids, text, confidence=confidence, session_id=session_id))


def t_act(inference_id: IdList | IdStr, action_type: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{2,40}$")],
          session_id: Session = None) -> CallToolResult:
    """Create a proposed (never executed) action based on one or more inferences."""
    return _exec(lambda: act(_ctx(), _agent(), inference_id, action_type, session_id=session_id))


def t_trace_provenance(node_id: IdStr) -> CallToolResult:
    """Upstream sources and downstream dependents of a node, with paths and edge directions."""
    return _exec(lambda: trace_provenance(_ctx(), node_id))


def t_explain_belief(node_id: IdStr) -> CallToolResult:
    """Why a fact, summary, inference or action exists, from graph paths only."""
    return _exec(lambda: explain_belief(_ctx(), node_id))


def t_find_derived(user_id: IdStr) -> CallToolResult:
    """Every node whose content or derivation depends on the user's personal data."""
    def run():
        _subject(user_id)
        return find_derived(_ctx(), user_id)
    return _exec(run)


def t_mark_superseded(old_fact_id: IdStr, new_fact_id: IdStr) -> CallToolResult:
    """Record that a newer fact replaces an older one; the old fact is kept and stays covered by erasure."""
    return _exec(lambda: mark_superseded(_ctx(), old_fact_id, new_fact_id))


def t_audit_forgotten(user_id: IdStr, deleted_node_ids: Annotated[list[IdStr] | None, Field(max_length=500)] = None,
                      text_terms: Annotated[list[Annotated[str, Field(min_length=2, max_length=80)]] | None,
                                            Field(max_length=5)] = None) -> CallToolResult:
    """Check the remaining graph for covered data about a user. Read-only; home tenant only."""
    def run():
        _subject(user_id)
        return audit_forgotten(_ctx(), user_id, deleted_node_ids=deleted_node_ids, text_terms=text_terms)
    return _exec(run)


def t_detect_leak(tenant_a: IdStr, tenant_b: IdStr, user_id: IdStr | None = None) -> CallToolResult:
    """Cross-tenant contamination between two tenants, with a remediation preview that is never executed."""
    def run():
        if tenant_b not in B.allowed_tenants:
            raise ToolError("FORBIDDEN_TENANT", "this server may not inspect that tenant")
        return detect_leak(_ctx(), tenant_a, tenant_b, authorization=_auditor_auth(), user_id=user_id)
    return _exec(run)


def t_forget_preview_only(user_id: IdStr, dry_run: bool = True) -> CallToolResult:
    """Preview what erasing a user would delete. This role can only dry-run; it never deletes."""
    def run():
        _subject(user_id)
        if not dry_run:
            raise ToolError("FORBIDDEN_DESTRUCTIVE", "this role may only preview an erasure (dry_run=true)")
        return forget(_ctx(), user_id, True, authorization=_auditor_auth())
    return _exec(run)


def t_forget_operator(user_id: IdStr, dry_run: bool = True,
                      preview_version: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = None,
                      reason: Annotated[str | None, Field(max_length=200)] = None,
                      authorization: AuthorizationClaim | None = None) -> CallToolResult:
    """Preview (dry_run=true) or perform (dry_run=false) an erasure. Confirming needs a matching
    preview_version, a reason and an authorization claim; the erasure policy validates everything."""
    def run():
        _subject(user_id)
        auth = Authorization(authorization.principal_id, authorization.role, tuple(authorization.tenant_ids)) \
            if authorization else None
        return forget(_ctx(), user_id, dry_run, authorization=auth, preview_version=preview_version, reason=reason)
    return _exec(run)


TOOL_FUNCS: dict[str, Callable] = {
    "remember": t_remember, "summarize": t_summarize, "infer": t_infer, "act": t_act,
    "trace_provenance": t_trace_provenance, "explain_belief": t_explain_belief, "find_derived": t_find_derived,
    "mark_superseded": t_mark_superseded, "audit_forgotten": t_audit_forgotten, "detect_leak": t_detect_leak,
}
assert set(TOOL_FUNCS) | {"forget"} == set(ALL_TOOLS)


# ---------- the server ----------
def _error_result(code: str, message: str, details: dict | None = None) -> CallToolResult:
    return _body({"error": {"code": code, "message": message, "details": details or {}}}, True)


class GuardedServer(MCPServer):
    """Adds strict argument checking and audit logging around every tool call."""

    async def _schemas(self) -> dict[str, set[str]]:
        if not hasattr(self, "_props"):
            self._props = {t.name: set((t.input_schema or {}).get("properties", {})) for t in await self.list_tools()}
        return self._props

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None):
        started = time.monotonic()
        meta = {}
        rc = getattr(context, "request_context", None)
        if rc is not None and getattr(rc, "meta", None):
            meta = dict(rc.meta)
        arguments = arguments or {}
        props = await self._schemas()
        status, code, result = "ok", None, None
        try:
            if name not in props:
                status, code = "rejected", "TOOL_NOT_EXPOSED"
                result = _error_result(code, f"'{name}' is not exposed by the {B.role} server")
            elif set(arguments) - props[name]:
                status, code = "rejected", "INVALID_ARGUMENTS"
                result = _error_result(code, f"unexpected argument(s): {sorted(set(arguments) - props[name])}")
            else:
                try:
                    result = await super().call_tool(name, arguments, context)
                except Exception as exc:
                    chain, cur = [], exc
                    while cur is not None and len(chain) < 5:
                        chain.append(cur)
                        cur = cur.__cause__ or cur.__context__
                    ve = next((c for c in chain if isinstance(c, ValidationError)), None)
                    if ve is not None:
                        status, code = "rejected", "INVALID_ARGUMENTS"
                        errs = [{"loc": [str(x) for x in e["loc"]], "type": e["type"]} for e in ve.errors()]
                        result = _error_result(code, "arguments failed schema validation", {"errors": errs})
                    else:
                        status, code = "error", "INTERNAL_ERROR"
                        result = _error_result(code, type(exc).__name__)
                if status == "ok" and getattr(result, "is_error", False):
                    status = "error"
                    code = ((result.structured_content or {}).get("error") or {}).get("code", "ERROR")
                    if code in ("FORBIDDEN_SUBJECT", "FORBIDDEN_POLICY", "FORBIDDEN_DESTRUCTIVE", "FORBIDDEN_TENANT",
                                "FORBIDDEN_ROLE", "UNAUTHORIZED"):
                        status = "rejected"
        finally:
            body = getattr(result, "structured_content", None) or {}
            B.log.event("tool_call", trace_id=meta.get("trace_id"), seq=meta.get("seq"), tool=name, role=B.role,
                        agent_id=AGENT_IDS.get(B.role), tenant=B.tenant, status=status, error_code=code,
                        ms=int((time.monotonic() - started) * 1000), arg_keys=sorted(arguments),
                        statements=len(body.get("cypher", [])) if status == "ok" else None,
                        warnings=len(body.get("warnings", [])) if status == "ok" else None,
                        reason=meta.get("reason"))
        return result


def build_server(binding: Binding) -> GuardedServer:
    global B
    B = binding
    app = GuardedServer(f"forgetting-agent-{binding.role}",
                        instructions=f"Graph tools for the {binding.role} role, tenant {binding.tenant}. Synthetic demo data.")
    for name in ROLE_TOOLS[binding.role]:
        if name == "forget":
            fn = t_forget_operator if binding.role == "operator" else t_forget_preview_only
        else:
            fn = TOOL_FUNCS[name]
        app.add_tool(fn, name=name, structured_output=False)
    return app


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--role", required=True, choices=sorted(ROLE_TOOLS))
    p.add_argument("--tenant", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--auditor-tenants", default="")
    p.add_argument("--run-id", default="run_unknown")
    a = p.parse_args(argv)
    names = json.loads(os.environ["MCP_GRAPH_NAMES"]) if os.environ.get("MCP_GRAPH_NAMES") else None
    log = AuditLog(os.environ.get("MCP_LOG_PATH") or None, a.run_id, f"mcp-server:{a.role}")
    allowed = tuple(t for t in a.auditor_tenants.split(",") if t) or (a.tenant,)
    binding = Binding(a.role, a.tenant, a.subject, allowed, names, os.environ.get("RECEIPT_DIR"), a.run_id, log)
    app = build_server(binding)
    log.event("server_start", role=a.role, tenant=a.tenant, tools=list(ROLE_TOOLS[a.role]))
    app.run("stdio")


if __name__ == "__main__":
    main()
