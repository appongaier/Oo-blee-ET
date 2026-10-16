"""Runs the three agents and the handoffs between them, over MCP.

Handoffs carry ids taken from tool results, never text written by a model, so an invented id cannot
travel from one agent to the next. Erasure is a deterministic step triggered by an authorized human or
service through the operator MCP server, never by an LLM. Every handoff and every tool call is written
to the MCP audit log, so a run's handoff sequence can be rebuilt from the log alone.
"""
from __future__ import annotations

import logging
import uuid

from app.agents.gateway import AgentSession, ToolGateway
from app.agents.llm import LLM
from app.agents.mcp_client import McpTransport, ServerBinding
from app.agents.runtime import run_agent
from app.config import get_settings
from app.mcp_support import AuditLog, McpToolError
from app.tenants import TENANTS

log = logging.getLogger("forgetting.agents")


def _claim(auth) -> dict:
    """Authorization as plain data for the operator server (duck-typed: principal_id, role, tenant_ids)."""
    return {"principal_id": auth.principal_id, "role": auth.role, "tenant_ids": list(auth.tenant_ids)}


class Orchestrator:
    def __init__(self, tenant_id: str, subject_user_id: str, llm: LLM | dict[str, LLM], *,
                 graph_names: dict[str, str] | None = None, receipt_dir: str | None = None,
                 log_path: str | None = None, run_id: str | None = None,
                 auditor_tenants: tuple[str, ...] | None = None):
        run_id = run_id or f"run_{uuid.uuid4().hex[:8]}"
        log_path = get_settings().mcp_log_path if log_path is None else log_path
        binding = ServerBinding(tenant_id, subject_user_id, run_id, auditor_tenants or tuple(sorted(TENANTS)),
                                graph_names, receipt_dir, log_path)
        self.transport = McpTransport(binding)
        self.audit = AuditLog(log_path or None, run_id, "orchestrator")
        self.llms = llm if isinstance(llm, dict) else {r: llm for r in ("concierge", "analyst", "auditor")}
        self.handoffs: list[dict] = []
        self._op_calls = 0
        self.session = AgentSession(tenant_id, subject_user_id, run_id=run_id)
        self.gateway = ToolGateway(self.session, self.transport, self.audit)
        try:                                       # resolve the subject's display name over MCP, like everything else
            out = self._operator("find_derived", {"user_id": subject_user_id})
            self.session.subject_display_name = out["result"]["user"]["display_name"]
        except Exception:
            self.close()
            raise

    # ---------- lifecycle ----------
    def close(self) -> None:
        self.transport.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------- plumbing ----------
    def _operator(self, tool: str, args: dict) -> dict:
        self._op_calls += 1
        trace = f"{self.session.run_id}-op{self._op_calls}"
        self.audit.event("operator_call", trace_id=trace, tool=tool, arg_keys=sorted(args))
        res = self.transport.get("operator").call(tool, args, {"trace_id": trace, "reason": "orchestrator step"})
        if not res["ok"]:
            e = res["error"]
            raise McpToolError(e.get("code", "ERROR"), e.get("message", ""), e.get("details"))
        return res["output"]

    def run(self, role: str, message: str, artifacts: dict | None = None) -> dict:
        aid = f"agent_{role}"
        self.audit.event("agent_turn_start", agent_id=aid)
        out = run_agent(role, self.gateway, self.llms[role], message, artifacts=artifacts)
        self.audit.event("agent_turn_end", agent_id=aid, tool_calls=len(out["tool_trace"]),
                         warnings=[w["code"] for w in out["warnings"]])
        return out

    def handoff(self, frm: str, to: str, reason: str, node_ids: list[str]) -> dict:
        h = {"seq": len(self.handoffs) + 1, "from": frm, "to": to, "reason": reason,
             "node_ids": list(node_ids), "at": ""}
        self.audit.event("handoff", seq=h["seq"], **{"from": frm}, to=to, reason=reason, node_ids=node_ids)
        self.handoffs.append(h)
        log.info("handoff %s -> %s (%d nodes): %s", frm, to, len(node_ids), reason)
        return h

    # ---------- ingest -> analyse -> audit ----------
    def run_pipeline(self, statements: list[str]) -> dict:
        name = self.session.subject_display_name
        bullet = "\n".join(f"- {s}" for s in statements)
        made: dict = {}
        concierge = self.run("concierge", f"{name} told you the following. Record each fact with remember, "
                             f"one call per fact, and nothing else:\n{bullet}", made)
        out = {"concierge": concierge, "created": made, "handoffs": self.handoffs}
        facts = made.get("fact_id", [])
        if not facts:
            out["stopped"] = "the Concierge recorded no facts, so there is nothing to hand off"
            return out

        self.handoff("agent_concierge", "agent_analyst", "new episodic facts recorded", facts)
        made2: dict = {}
        analyst = self.run("analyst", f"These facts were just recorded for {name}: {', '.join(facts)}. Read them "
                           "with find_derived, then create a summary, any inference they directly support (with "
                           "an honest confidence), and propose an action only if an inference supports it.", made2)
        out["analyst"] = analyst
        derived = [i for k in ("summary_id", "inference_id", "action_id") for i in made2.get(k, [])]
        out["created"] = {**made, **made2}
        if not derived:
            out["stopped"] = "the Analyst created no derived nodes, so there is nothing to audit"
            return out

        self.handoff("agent_analyst", "agent_auditor", "derived nodes created", derived)
        out["auditor"] = self.run("auditor", "Verify the provenance of these nodes with explain_belief and report "
                                  f"whether each is supported by the graph: {', '.join(derived)}.")
        return out

    # ---------- erasure ----------
    def preview_erasure(self) -> dict:
        """The Auditor narrates the dry run; the structured preview itself comes straight from the tool."""
        name = self.session.subject_display_name
        plan = self._operator("forget", {
            "user_id": self.session.subject_user_id, "dry_run": True,
            "authorization": {"principal_id": "agent_auditor", "role": "auditor",
                              "tenant_ids": [self.session.tenant_id]}})
        preview = plan["result"]["preview"]
        self.handoff("orchestrator", "agent_auditor", "erasure preview requested", preview["node_ids"])
        auditor = self.run("auditor", f"Preview erasing {name} with forget_preview and summarize it: counts, "
                           "shared nodes, kept nodes and any cross-tenant findings.")
        return {"preview": preview, "can_execute": plan["result"]["can_execute"], "auditor": auditor,
                "handoffs": self.handoffs}

    def execute_erasure(self, preview_version: str, reason: str, authorization) -> dict:
        """Deterministic: validated by the erasure policy, never decided by an LLM. Raises McpToolError
        if the authorization, preview or graph state does not hold up."""
        res = self._operator("forget", {
            "user_id": self.session.subject_user_id, "dry_run": False, "preview_version": preview_version,
            "reason": reason, "authorization": _claim(authorization)})
        receipt = res["result"]["receipt"]
        self.session.deleted_node_ids = receipt["deleted_node_ids"]
        others = [t for t in self.transport.binding.auditor_tenants if t != self.session.tenant_id]
        self.handoff("orchestrator", "agent_auditor", "erasure completed; verify", receipt["deleted_node_ids"])
        auditor = self.run("auditor", f"Erasure job {receipt['job_id']} finished. Run audit_forgotten and "
                           f"detect_leak for tenant(s) {', '.join(others) or 'none'}, then report what the graph "
                           "audit shows and what it cannot prove.")
        return {"receipt": receipt, "auditor": auditor, "handoffs": self.handoffs}
