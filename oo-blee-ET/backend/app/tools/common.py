"""Shared plumbing for the graph tools: context, query recording, validation, errors.

Every tool returns {"result", "cypher", "params", "graph", "warnings"}. "cypher" and "params"
are parallel lists of every statement actually executed, in order.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from redis.exceptions import RedisError

from app.config import get_settings
from app.schema import SENSITIVITIES, TIERS
from app.tenants import graph_name_for, validate_graph_name

MAX_DEPTH = 8          # traversal depth limit (hops) for provenance and dependency walks
ROW_CAP = 2000         # maximum path rows read from one traversal query
TEXT_MAX = 1000
DERIVATION = "SUMMARIZES|INFERRED_FROM|BASED_ON"       # child -> parent belief edges
UPSTREAM = DERIVATION + "|DERIVED_FROM|ABOUT"          # plus fact -> source / subject
ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,120}$")
SESSION_RE = re.compile(r"^[a-z0-9_]{3,40}$")
ACTION_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
ROLES = ("data_subject", "tenant_admin", "auditor")


class ToolError(Exception):
    """A tool refused or failed. Carries a stable code; nothing was changed unless stated."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.details = code, message, details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class Authorization:
    """Claims about who is calling. This is an authorization *check on supplied claims*;
    the prototype has no authentication."""
    principal_id: str
    role: str                    # data_subject | tenant_admin | auditor
    tenant_ids: tuple[str, ...]


@dataclass
class ToolContext:
    client: Any
    tenant_id: str
    graph_names: dict[str, str] | None = None   # tests use throwaway graph names
    receipt_dir: str | None = None              # None -> settings; "" -> do not write
    max_depth: int = MAX_DEPTH

    def graph_name_of(self, tenant_id: str) -> str:
        if self.graph_names and tenant_id in self.graph_names:
            return validate_graph_name(self.graph_names[tenant_id])
        return graph_name_for(tenant_id)

    @property
    def graph(self):
        return self.client.select_graph(self.graph_name_of(self.tenant_id))

    def graph_for(self, tenant_id: str):
        return self.client.select_graph(self.graph_name_of(tenant_id))

    def resolved_receipt_dir(self) -> str:
        return get_settings().receipt_dir if self.receipt_dir is None else self.receipt_dir


class Recorder:
    """Runs queries against one graph and remembers exactly what was executed."""

    def __init__(self, graph):
        self.graph = graph
        self.cypher: list[str] = []
        self.params: list[dict] = []

    def _run(self, fn, q: str, p: dict | None):
        self.cypher.append(" ".join(q.split()))
        self.params.append(p or {})
        try:
            return fn(q, p or {}).result_set
        except RedisError as exc:
            raise ToolError("GRAPH_ERROR", f"{type(exc).__name__}: {str(exc)[:160]}") from exc

    def read(self, q: str, p: dict | None = None) -> list:
        return self._run(self.graph.ro_query, q, p)

    def write(self, q: str, p: dict | None = None) -> list:
        return self._run(self.graph.query, q, p)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def warn(code: str, message: str, node_id: str | None = None, severity: str = "warning") -> dict:
    w = {"code": code, "severity": severity, "message": message}
    if node_id:
        w["node_id"] = node_id
    return w


# ---------- validation ----------
def check_id(value: Any, name: str = "id") -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ToolError("INVALID_INPUT", f"{name} must match {ID_RE.pattern}")
    return value


def check_id_list(values: Any, name: str, max_items: int = 50) -> list[str]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= max_items:
        raise ToolError("INVALID_INPUT", f"{name} must be a list of 1-{max_items} ids")
    ids = [check_id(v, name) for v in values]
    if len(set(ids)) != len(ids):
        raise ToolError("INVALID_INPUT", f"{name} contains duplicates")
    return ids


def check_tier(tier: Any) -> str:
    if tier not in TIERS:
        raise ToolError("INVALID_INPUT", f"tier must be one of {list(TIERS)}")
    return tier


def check_sensitivity(value: Any) -> str:
    if value not in SENSITIVITIES:
        raise ToolError("INVALID_INPUT", f"sensitivity must be one of {list(SENSITIVITIES)}")
    return value


def check_text(text: Any) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
        raise ToolError("INVALID_INPUT", f"text must be a non-empty string of at most {TEXT_MAX} characters")
    return text.strip()


def check_session(session_id: str | None) -> str:
    if session_id is None:
        return f"sess_{uuid.uuid4().hex[:10]}"
    if not isinstance(session_id, str) or not SESSION_RE.fullmatch(session_id):
        raise ToolError("INVALID_INPUT", f"session_id must match {SESSION_RE.pattern}")
    return session_id


def get_agent(rec: Recorder, agent_id: str, allowed_roles: set[str]) -> str:
    check_id(agent_id, "agent_id")
    rows = rec.read("MATCH (a:Agent {id: $id}) RETURN a.role", {"id": agent_id})
    if not rows:
        raise ToolError("AGENT_NOT_FOUND", f"agent {agent_id} not found in this tenant graph")
    role = rows[0][0]
    if role not in allowed_roles:
        raise ToolError("ROLE_NOT_ALLOWED", f"agent role '{role}' may not perform this operation")
    return role


def get_user(rec: Recorder, ctx: ToolContext, user_id: str) -> dict:
    check_id(user_id, "user_id")
    rows = rec.read("MATCH (u:User {id: $id}) RETURN u.tenant_id, u.display_name", {"id": user_id})
    if not rows:
        raise ToolError("USER_NOT_FOUND", f"user {user_id} not found in tenant {ctx.tenant_id}")
    if rows[0][0] != ctx.tenant_id:
        raise ToolError("TENANT_MISMATCH", f"user {user_id} does not belong to tenant {ctx.tenant_id}")
    return {"id": user_id, "display_name": rows[0][1]}


def require_authorization(auth: Authorization | None, tenant_ids: list[str], roles: set[str],
                          subject_user_id: str | None = None) -> Authorization:
    if auth is None:
        raise ToolError("UNAUTHORIZED", "an authorization is required for this operation")
    if auth.role not in ROLES or auth.role not in roles:
        raise ToolError("FORBIDDEN_ROLE", f"role '{auth.role}' may not perform this operation")
    missing = [t for t in tenant_ids if t not in auth.tenant_ids]
    if missing:
        raise ToolError("FORBIDDEN_TENANT", f"authorization does not cover tenant(s): {missing}")
    if auth.role == "data_subject" and subject_user_id is not None \
            and auth.principal_id != subject_user_id:
        raise ToolError("FORBIDDEN_SUBJECT", "a data subject may only act on their own data")
    return auth


# ---------- graph helpers ----------
def node_dict(node) -> dict:
    props = dict(node.properties)
    props["label"] = node.labels[0]
    return props


def fetch_nodes(rec: Recorder, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    rows = rec.read("MATCH (n) WHERE n.id IN $ids RETURN n", {"ids": list(ids)})
    return {r[0].properties["id"]: node_dict(r[0]) for r in rows}


def fragment(rec: Recorder, ids: list[str], strip_text: bool = False) -> dict:
    """The nodes with these ids and the edges between them, as read from the graph now."""
    ids = sorted(set(ids))
    nodes = fetch_nodes(rec, ids)
    edges = []
    if ids:
        rows = rec.read(
            "MATCH (a)-[r]->(b) WHERE a.id IN $ids AND b.id IN $ids RETURN a.id, type(r), b.id",
            {"ids": ids},
        )
        edges = sorted(({"src": a, "rel": r, "dst": b} for a, r, b in rows),
                       key=lambda e: (e["src"], e["rel"], e["dst"]))
    out_nodes = []
    for nid in ids:
        if nid in nodes:
            n = dict(nodes[nid])
            if strip_text:
                n.pop("text", None)
            out_nodes.append(n)
    return {"nodes": out_nodes, "edges": edges}


def build_result(rec: Recorder, result: dict, graph: dict | None = None,
                 warnings: list[dict] | None = None) -> dict:
    return {
        "result": result,
        "cypher": list(rec.cypher),
        "params": list(rec.params),
        "graph": graph if graph is not None else {"nodes": [], "edges": []},
        "warnings": warnings or [],
    }


def depth_limit(ctx: ToolContext) -> int:
    d = ctx.max_depth
    if not isinstance(d, int) or not 1 <= d <= 20:
        raise ToolError("INVALID_INPUT", "max_depth must be an integer between 1 and 20")
    return d
