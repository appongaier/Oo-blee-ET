"""HTTP API for the UI. The browser never talks to FalkorDB or MCP directly.

Tool operations go over MCP (see lifecycle.Runtime). Authorization comes from the `acting_as` claim in the
request: a demo of the rules, not authentication.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agents import Orchestrator
from app.constants import DISCLAIMER
from app.graph_view import export_graph, list_users
from app.lifecycle import VIEWER, Runtime, get_runtime
from app.mcp_support import McpToolError
from app.seed import seed_all
from app.tenants import TENANTS, UnknownTenantError

router = APIRouter(prefix="/api")

Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:\-]{1,120}$")]
Tenant = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,30}$")]

STATUS = {"UNAUTHORIZED": 401, "FORBIDDEN_ROLE": 403, "FORBIDDEN_TENANT": 403, "FORBIDDEN_SUBJECT": 403,
          "FORBIDDEN_DESTRUCTIVE": 403, "FORBIDDEN_POLICY": 403, "USER_NOT_FOUND": 404, "NODE_NOT_FOUND": 404,
          "TOOL_NOT_EXPOSED": 404, "PREVIEW_STALE": 409, "ERASURE_BLOCKED": 409, "DELETE_ABORTED": 409,
          "PREVIEW_REQUIRED": 422, "INVALID_INPUT": 422, "INVALID_ARGUMENTS": 422, "UNSUPPORTED_NODE_TYPE": 422,
          "LLM_NOT_CONFIGURED": 503, "UNKNOWN_TENANT": 404, "NOT_SEEDED": 404, "DEMO_CONTROLS_OFF": 403, "MCP_START_FAILED": 502, "MCP_TRANSPORT_ERROR": 502, "MCP_PROTOCOL_ERROR": 502}


def error_response(exc: McpToolError) -> JSONResponse:
    return JSONResponse(status_code=STATUS.get(exc.code, 400), content={"error": exc.to_dict()})


class ActingAs(BaseModel):
    """Who is asking (a claim). The tenant is taken from the request, not from the client."""
    model_config = ConfigDict(extra="forbid")
    principal_id: Id
    role: Literal["data_subject", "tenant_admin", "auditor"]


class UserRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant: Tenant
    user_id: Id


class MemoryIn(UserRef):
    text: Annotated[str, Field(min_length=1, max_length=1000)]
    sensitivity: Literal["low", "medium", "high"] = "low"


class PreviewIn(UserRef):
    acting_as: ActingAs


class ExecuteIn(PreviewIn):
    preview_version: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    reason: Annotated[str, Field(min_length=1, max_length=200)]


class AuditIn(UserRef):
    deleted_node_ids: Annotated[list[Id], Field(max_length=500)] | None = None
    text_terms: Annotated[list[Annotated[str, Field(min_length=2, max_length=80)]], Field(max_length=5)] | None = None


class LeaksIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_a: Tenant
    tenant_b: Tenant


class AskIn(UserRef):
    role: Literal["concierge", "analyst", "auditor"]
    message: Annotated[str, Field(min_length=1, max_length=1000)]


class PipelineIn(UserRef):
    statements: Annotated[list[Annotated[str, Field(min_length=1, max_length=300)]], Field(min_length=1, max_length=6)]


def _tenant(rt: Runtime, tenant: str) -> str:
    try:
        return rt.graph_name(tenant)
    except UnknownTenantError:
        raise McpToolError("UNKNOWN_TENANT", f"unknown tenant {tenant!r}") from None


def _claim(a: ActingAs, tenant: str) -> dict:
    return {"principal_id": a.principal_id, "role": a.role, "tenant_ids": [tenant]}


# ---------- reads ----------
@router.get("/config")
def config(rt: Runtime = Depends(get_runtime)) -> dict:
    tenants = []
    for t in rt.tenants():
        graph = _tenant(rt, t)
        seeded = rt.seeded(t)
        tenants.append({"id": t, "graph": graph, "seeded": seeded, "users": list_users(rt.db, graph) if seeded else []})
    return {"tenants": tenants, "llm_available": rt.llm_available(), "demo_controls": rt.demo_controls,
            "disclaimer": DISCLAIMER}


@router.get("/graph/{tenant}")
def graph(tenant: Tenant, rt: Runtime = Depends(get_runtime)) -> dict:
    name = _tenant(rt, tenant)
    if not rt.seeded(tenant):
        raise McpToolError("NOT_SEEDED", "this tenant graph does not exist yet; reset the demo data")
    return {"tenant": tenant, **export_graph(rt.db, name)}


@router.get("/nodes/{tenant}/{node_id}/explain")
def explain(tenant: Tenant, node_id: Id, rt: Runtime = Depends(get_runtime)) -> dict:
    _tenant(rt, tenant)
    return rt.call("auditor", tenant, VIEWER, "explain_belief", {"node_id": node_id}, "node details")


@router.get("/nodes/{tenant}/{node_id}/trace")
def trace(tenant: Tenant, node_id: Id, rt: Runtime = Depends(get_runtime)) -> dict:
    _tenant(rt, tenant)
    return rt.call("auditor", tenant, VIEWER, "trace_provenance", {"node_id": node_id}, "node details")


# ---------- writes ----------
@router.post("/memory")
def add_memory(body: MemoryIn, rt: Runtime = Depends(get_runtime)) -> dict:
    """A person tells the Concierge something. The text goes through the Concierge MCP server as-is."""
    _tenant(rt, body.tenant)
    return rt.call("concierge", body.tenant, body.user_id, "remember",
                   {"user_id": body.user_id, "text": body.text, "tier": "episodic", "sensitivity": body.sensitivity},
                   "memory entered in the UI")


@router.post("/demo/reset")
def demo_reset(rt: Runtime = Depends(get_runtime)) -> dict:
    if not rt.demo_controls:
        raise McpToolError("DEMO_CONTROLS_OFF", "reset is disabled on this server")
    return {"graphs": seed_all(rt.db, reset=True, graph_names=rt.graph_names)}


# ---------- erasure lifecycle ----------
@router.post("/erasure/preview")
def erasure_preview(body: PreviewIn, rt: Runtime = Depends(get_runtime)) -> dict:
    _tenant(rt, body.tenant)
    return rt.call("operator", body.tenant, body.user_id, "forget",
                   {"user_id": body.user_id, "dry_run": True, "authorization": _claim(body.acting_as, body.tenant)},
                   "erasure preview")


@router.post("/erasure/execute")
def erasure_execute(body: ExecuteIn, rt: Runtime = Depends(get_runtime)) -> dict:
    _tenant(rt, body.tenant)
    return rt.call("operator", body.tenant, body.user_id, "forget",
                   {"user_id": body.user_id, "dry_run": False, "preview_version": body.preview_version,
                    "reason": body.reason, "authorization": _claim(body.acting_as, body.tenant)},
                   "confirmed erasure")


@router.post("/audit")
def audit(body: AuditIn, rt: Runtime = Depends(get_runtime)) -> dict:
    _tenant(rt, body.tenant)
    args = {"user_id": body.user_id}
    if body.deleted_node_ids:
        args["deleted_node_ids"] = body.deleted_node_ids
    if body.text_terms:
        args["text_terms"] = body.text_terms
    return rt.call("auditor", body.tenant, body.user_id, "audit_forgotten", args, "post-erasure audit")


@router.post("/leaks")
def leaks(body: LeaksIn, rt: Runtime = Depends(get_runtime)) -> dict:
    _tenant(rt, body.tenant_a)
    _tenant(rt, body.tenant_b)
    return rt.call("auditor", body.tenant_a, VIEWER, "detect_leak",
                   {"tenant_a": body.tenant_a, "tenant_b": body.tenant_b}, "cross-tenant check")


# ---------- agents (need a real LLM) ----------
def _orchestrator(rt: Runtime, tenant: str, user_id: str) -> Orchestrator:
    _tenant(rt, tenant)
    return Orchestrator(tenant, user_id, rt.make_llm(), graph_names=rt.graph_names, receipt_dir=rt.receipt_dir,
                        log_path=rt.log_path, run_id=rt.run_id)


@router.post("/agents/ask")
def agents_ask(body: AskIn, rt: Runtime = Depends(get_runtime)) -> dict:
    o = _orchestrator(rt, body.tenant, body.user_id)
    try:
        return {"response": o.run(body.role, body.message), "handoffs": o.handoffs}
    finally:
        o.close()


@router.post("/agents/pipeline")
def agents_pipeline(body: PipelineIn, rt: Runtime = Depends(get_runtime)) -> dict:
    o = _orchestrator(rt, body.tenant, body.user_id)
    try:
        return o.run_pipeline(body.statements)
    finally:
        o.close()
