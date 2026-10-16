"""Read tools: trace_provenance, explain_belief, find_derived.

Traversals are variable-length Cypher walks limited to ctx.max_depth hops (default 8). Each
walk asks for one hop more than the limit so it can tell "complete" from "truncated".
Nothing here invents provenance: a missing edge is reported, never assumed.
"""
from __future__ import annotations

from app.tools.common import (DERIVATION, ROW_CAP, UPSTREAM, Recorder, ToolContext, ToolError,
                              build_result, check_id, depth_limit, fetch_nodes, fragment,
                              get_user, warn)

BELIEF_LABELS = ("Fact", "Summary", "Inference", "Action")
TRACEABLE_LABELS = BELIEF_LABELS + ("Source", "User")


# ---------- integrity ----------
def integrity_warnings(rec: Recorder, ctx: ToolContext, ids: list[str]) -> list[dict]:
    """Missing or ambiguous provenance for these nodes. Absence is a finding, not proof of
    independence."""
    ids = sorted(set(ids))
    if not ids:
        return []
    rows = rec.read(
        """
        UNWIND $ids AS nid MATCH (n {id: nid})
        RETURN n.id, labels(n)[0], n.tenant_id,
               size([(n)-[:DERIVED_FROM]->() | 1]), size([(n)-[:ABOUT]->() | 1]),
               size([(n)-[:SUMMARIZES|INFERRED_FROM|BASED_ON]->() | 1]),
               size([(n)<-[:CONTAINS]-() | 1])
        """,
        {"ids": ids},
    )
    out = []
    for nid, label, tenant, n_src, n_about, n_parents, n_cont in rows:
        if label in ("User", "Fact", "Summary", "Inference", "Action", "Memory") and tenant != ctx.tenant_id:
            out.append(warn("TENANT_MISMATCH", f"{label} has tenant_id {tenant!r}, expected {ctx.tenant_id!r}",
                            nid, "blocking"))
        if label == "Fact":
            if n_src == 0:
                out.append(warn("MISSING_SOURCE", "fact has no DERIVED_FROM source", nid))
            if n_about == 0:
                out.append(warn("MISSING_SUBJECT", "fact has no ABOUT subject", nid))
            elif n_about > 1:
                out.append(warn("AMBIGUOUS_SUBJECT", "fact is ABOUT more than one user", nid))
        if label in ("Summary", "Inference", "Action") and n_parents == 0:
            out.append(warn("MISSING_PARENT", f"{label} has no parent belief edge", nid))
        if label in BELIEF_LABELS and n_cont == 0:
            out.append(warn("MISSING_CONTAINER", f"{label} is not contained in any Memory", nid))
    return out


# ---------- traversal ----------
def _walk(rec: Recorder, node_id: str, rels: str, upstream: bool, depth: int) -> tuple[list[dict], bool]:
    """All simple paths from node_id up to `depth` hops. Returns (paths, truncated)."""
    arrow = f"-[:{rels}*1..{depth + 1}]->" if upstream else f"<-[:{rels}*1..{depth + 1}]-"
    rows = rec.read(
        f"MATCH p=(n {{id: $id}}){arrow}(x) "
        "RETURN [m IN nodes(p) | m.id], [m IN nodes(p) | labels(m)[0]], [r IN relationships(p) | type(r)] "
        f"LIMIT {ROW_CAP + 1}",
        {"id": node_id},
    )
    truncated = len(rows) > ROW_CAP
    paths = []
    for ids, labels, rel_types in rows[:ROW_CAP]:
        if len(rel_types) > depth:
            truncated = True      # something exists beyond the limit
            continue
        if upstream:
            edges = [{"src": ids[i], "rel": rel_types[i], "dst": ids[i + 1]} for i in range(len(rel_types))]
        else:
            edges = [{"src": ids[i + 1], "rel": rel_types[i], "dst": ids[i]} for i in range(len(rel_types))]
        paths.append({"nodes": [{"id": i, "type": t} for i, t in zip(ids, labels)], "edges": edges})
    return paths, truncated


def _require_node(rec: Recorder, ctx: ToolContext, node_id: str, labels: tuple[str, ...]) -> dict:
    check_id(node_id, "node_id")
    node = fetch_nodes(rec, [node_id]).get(node_id)
    if node is None:
        raise ToolError("NODE_NOT_FOUND", f"node {node_id} not found in tenant {ctx.tenant_id}")
    if node["label"] not in labels:
        raise ToolError("UNSUPPORTED_NODE_TYPE", f"{node['label']} nodes are not supported here")
    return node


def _node_ids(paths: list[dict], origin: str | None = None) -> list[str]:
    return sorted({n["id"] for p in paths for n in p["nodes"]} - {origin})


def trace_provenance(ctx: ToolContext, node_id: str) -> dict:
    """Upstream sources and downstream dependents of a Fact, Summary, Inference, Action,
    Source or User, with paths, edge directions and node types."""
    rec, depth = Recorder(ctx.graph), depth_limit(ctx)
    node = _require_node(rec, ctx, node_id, TRACEABLE_LABELS)
    up, up_trunc = _walk(rec, node_id, UPSTREAM, True, depth)
    down, down_trunc = _walk(rec, node_id, UPSTREAM, False, depth)
    all_ids = sorted({node_id, *_node_ids(up), *_node_ids(down)})
    warnings = integrity_warnings(rec, ctx, [i for i in all_ids])
    for trunc, name in ((up_trunc, "upstream"), (down_trunc, "downstream")):
        if trunc:
            warnings.append(warn("TRUNCATED_TRAVERSAL",
                                 f"{name} traversal stopped at {depth} hops; the result is incomplete", node_id))
    result = {
        "node": node, "max_depth": depth,
        "upstream": {"paths": up, "node_ids": _node_ids(up, node_id), "truncated": up_trunc},
        "downstream": {"paths": down, "node_ids": _node_ids(down, node_id), "truncated": down_trunc},
        "complete": not (up_trunc or down_trunc),
    }
    return build_result(rec, result, fragment(rec, all_ids), warnings)


def explain_belief(ctx: ToolContext, node_id: str) -> dict:
    """Why does this Fact/Summary/Inference/Action exist? Built only from graph paths."""
    rec, depth = Recorder(ctx.graph), depth_limit(ctx)
    node = _require_node(rec, ctx, node_id, BELIEF_LABELS)
    paths, truncated = _walk(rec, node_id, UPSTREAM, True, depth)
    ids = sorted({node_id, *_node_ids(paths)})
    beliefs = [i for i in ids if i == node_id or any(
        n["id"] == i and n["type"] in BELIEF_LABELS for p in paths for n in p["nodes"])]
    warnings = integrity_warnings(rec, ctx, ids)
    if truncated:
        warnings.append(warn("TRUNCATED_TRAVERSAL", f"upstream traversal stopped at {depth} hops", node_id))

    rows = rec.read(
        "MATCH (m:Memory)-[:CONTAINS]->(n) WHERE n.id IN $ids "
        "OPTIONAL MATCH (a:Agent)-[:WROTE]->(m) RETURN n.id, m.id, m.tier, m.session_id, a.id",
        {"ids": beliefs},
    )
    memories = [{"node_id": n, "memory_id": m, "tier": t, "session_id": s, "writer_agent_id": a}
                for n, m, t, s, a in rows]
    authors = fetch_nodes(rec, beliefs)
    agents = sorted({a["agent_id"] for a in authors.values() if a.get("agent_id")}
                    | {m["writer_agent_id"] for m in memories if m["writer_agent_id"]})
    direct = [{"id": e["dst"], "rel": e["rel"], "type": next(n["type"] for n in p["nodes"] if n["id"] == e["dst"])}
              for p in paths for e in p["edges"][:1] if e["src"] == node_id]
    direct = [dict(t) for t in {tuple(sorted(d.items())) for d in direct}]
    sources = sorted({p["nodes"][-1]["id"] for p in paths if p["nodes"][-1]["type"] == "Source"})
    subjects = sorted({p["nodes"][-1]["id"] for p in paths if p["nodes"][-1]["type"] == "User"})
    missing = [w for w in warnings if w["code"].startswith("MISSING_") or w["code"] == "AMBIGUOUS_SUBJECT"]
    supported = bool(sources) and not missing and not truncated

    label = node["label"]
    if supported:
        explanation = (f"{node_id} is a {label} written by {node.get('agent_id', 'an unknown agent')}. "
                       f"It has {len(direct)} direct parent(s) and traces back to source(s) "
                       f"{', '.join(sources)} about user(s) {', '.join(subjects) or 'unknown'}.")
    else:
        explanation = (f"Evidence unavailable: the graph does not give a complete supporting path for "
                       f"{node_id}. See warnings for what is missing.")
    result = {"node": node, "direct_parents": sorted(direct, key=lambda d: d["id"]), "paths": paths,
              "sources": sources, "subjects": subjects, "agents": agents, "memories": memories,
              "supported": supported, "explanation": explanation, "max_depth": depth,
              "complete": not truncated}
    return build_result(rec, result, fragment(rec, ids), warnings)


# ---------- dependency discovery ----------
def find_derived_core(rec: Recorder, ctx: ToolContext, user_id: str) -> dict:
    """Covered personal data for a user under the documented policy.

    A: facts ABOUT the user (superseded ones included).
    B: every node reachable downstream of those facts through SUMMARIZES / INFERRED_FROM /
       BASED_ON, at any depth up to the limit. One covered parent is enough.
    """
    depth = depth_limit(ctx)
    user = get_user(rec, ctx, user_id)
    seed_rows = rec.read(
        "MATCH (f:Fact)-[:ABOUT]->(:User {id: $uid}) "
        "RETURN f.id, size([(f)-[:SUPERSEDED_BY]->() | 1]) ORDER BY f.id", {"uid": user_id})
    seeds = [{"id": r[0], "superseded": bool(r[1])} for r in seed_rows]
    seed_ids = [s["id"] for s in seeds]

    derived: dict[str, dict] = {}
    truncated = False
    if seed_ids:
        rows = rec.read(
            f"UNWIND $seeds AS sid MATCH p=(f:Fact {{id: sid}})<-[:{DERIVATION}*1..{depth + 1}]-(d) "
            "RETURN [m IN nodes(p) | m.id], [m IN nodes(p) | labels(m)[0]] "
            f"LIMIT {ROW_CAP + 1}", {"seeds": seed_ids})
        truncated = len(rows) > ROW_CAP
        for ids, labels in rows[:ROW_CAP]:
            if len(ids) - 1 > depth:
                truncated = True
                continue
            entry = derived.setdefault(ids[-1], {"id": ids[-1], "label": labels[-1], "paths": []})
            if ids not in entry["paths"]:
                entry["paths"].append(ids)

    others: dict[str, list[str]] = {}
    if derived:
        rows = rec.read(
            f"UNWIND $dids AS did MATCH (d {{id: did}})-[:{DERIVATION}*1..{depth}]->(f:Fact)-[:ABOUT]->(u:User) "
            "WHERE u.id <> $uid RETURN DISTINCT did, u.id", {"dids": sorted(derived), "uid": user_id})
        for did, uid in rows:
            others.setdefault(did, []).append(uid)
    for did, entry in derived.items():
        entry["depends_on_other_users"] = sorted(others.get(did, []))

    all_ids = sorted({*seed_ids, *derived})
    warnings = integrity_warnings(rec, ctx, all_ids)
    if truncated:
        warnings.append(warn("TRUNCATED_TRAVERSAL",
                             f"dependency walk stopped at {depth} hops; covered data may be missing", None, "blocking"))
    for r in rec.read("MATCH (f:Fact) WHERE NOT (f)-[:ABOUT]->(:User) RETURN f.id"):
        warnings.append(warn("UNATTRIBUTED_FACT",
                             "fact has no subject; it cannot be tied to any user and is not covered", r[0]))
    return {"user": user, "seeds": seeds, "derived": derived, "truncated": truncated, "warnings": warnings,
            "max_depth": depth, "node_ids": all_ids}


def find_derived(ctx: ToolContext, user_id: str) -> dict:
    """List every node whose content or derivation depends on the user's personal data."""
    rec = Recorder(ctx.graph)
    core = find_derived_core(rec, ctx, user_id)
    derived = sorted(core["derived"].values(), key=lambda d: (d["label"], d["id"]))
    result = {"user_id": user_id, "tenant_id": ctx.tenant_id, "user": core["user"], "max_depth": core["max_depth"],
              "user_originated": core["seeds"], "derived": derived, "node_ids": core["node_ids"],
              "truncated": core["truncated"], "complete": not core["truncated"]}
    return build_result(rec, result, fragment(rec, [*core["node_ids"], user_id]), core["warnings"])
