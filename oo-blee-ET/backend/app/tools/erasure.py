"""Erasure tools: forget, audit_forgotten, detect_leak.

Policy (see data/graph-model.md):
  A  user-originated: facts ABOUT the user (superseded included) and the User node.
  B  derived: any Summary/Inference/Action with at least one covered ancestor.
  C  independent: everything else is kept.
  D  metadata: a Source goes only if no surviving fact uses it; a Memory only if it ends up
     empty; Agents and Tenants stay. Receipts carry ids and types, never content.
Deletion is one guarded Cypher statement. There are no multi-statement transactions.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter

from app.tenants import graph_name_for
from app.tools.common import (DERIVATION, ROW_CAP, Authorization, Recorder, ToolContext, ToolError,
                              build_result, check_id, depth_limit, fetch_nodes, fragment, new_id,
                              now_iso, require_authorization, warn)
from app.tools.provenance import find_derived_core, integrity_warnings

POLICY = {
    "A_user_originated": "Facts ABOUT the user, superseded ones included, and the User node.",
    "B_derived": "Every summary, inference or action with at least one covered ancestor, to the "
                 "depth limit. One covered parent is enough, so nodes that mix several users' data "
                 "are deleted rather than rewritten.",
    "C_independent": "Everything else is kept: other users' facts and anything that only sits "
                     "nearby in the graph.",
    "D_metadata": "A Source is deleted only if no surviving fact uses it. A Memory is deleted only "
                  "if it ends up empty. Agents and Tenant nodes are never deleted. Receipts hold "
                  "ids and types, never content.",
    "fail_closed": "A truncated traversal or a tenant mismatch on a covered node blocks deletion.",
    "cross_tenant": "Copies in other tenants are reported, never deleted by this tool.",
}
LIMITATIONS = ("A graph audit checks only the FalkorDB graphs listed in its scope. It does not prove "
               "deletion from logs, backups, caches, search indexes or model-provider storage, and "
               "it is not a statement of legal compliance.")
CATEGORY = {"Fact": "A_user_originated", "User": "A_user_originated", "Summary": "B_derived",
            "Inference": "B_derived", "Action": "B_derived", "Source": "D_provenance_metadata",
            "Memory": "D_provenance_metadata"}


def _token(tenant_id: str, user_id: str, nodes: dict[str, str], edges: list[tuple]) -> str:
    blob = json.dumps({"tenant": tenant_id, "user": user_id, "nodes": sorted(nodes.items()),
                       "edges": sorted(edges)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------- the plan ----------
def compute_plan(rec: Recorder, ctx: ToolContext, user_id: str) -> tuple[dict, str]:
    """Deterministic deletion set for a user. Read-only. Returns (plan, display_name); the
    display name is for the post-deletion text audit only and is never put in the plan."""
    core = find_derived_core(rec, ctx, user_id)
    seeds = [s["id"] for s in core["seeds"]]
    derived = core["derived"]
    content = sorted({*seeds, *derived})
    warnings = list(core["warnings"])

    cand_sources, kept_sources = [], []
    if seeds:
        for sid, stype, others in rec.read(
                "UNWIND $fids AS fid MATCH (f:Fact {id: fid})-[:DERIVED_FROM]->(s:Source) "
                "WITH DISTINCT s OPTIONAL MATCH (o:Fact)-[:DERIVED_FROM]->(s) WHERE NOT o.id IN $fids "
                "RETURN s.id, s.type, count(o)", {"fids": seeds}):
            (kept_sources if others else cand_sources).append(sid)
    cand_memories, kept_memories = [], []
    if content:
        for mid, others in rec.read(
                "UNWIND $cids AS cid MATCH (m:Memory)-[:CONTAINS]->(c {id: cid}) WITH DISTINCT m "
                "OPTIONAL MATCH (m)-[:CONTAINS]->(o) WHERE NOT o.id IN $cids RETURN m.id, count(o)",
                {"cids": content}):
            (kept_memories if others else cand_memories).append(mid)

    node_ids = sorted({*content, user_id, *cand_sources, *cand_memories})
    nodes = fetch_nodes(rec, node_ids)
    labels = {i: nodes[i]["label"] for i in node_ids if i in nodes}
    edge_rows = rec.read(
        "MATCH (a)-[r]->(b) WHERE a.id IN $ids OR b.id IN $ids RETURN DISTINCT a.id, type(r), b.id",
        {"ids": node_ids})
    edges = sorted({(a, r, b) for a, r, b in edge_rows})

    touched_mem = sorted({*cand_memories, *kept_memories})
    writers = rec.read("MATCH (a:Agent)-[:WROTE]->(m:Memory) WHERE m.id IN $mids RETURN DISTINCT a.id",
                       {"mids": touched_mem}) if touched_mem else []
    agents = sorted({nodes[i].get("agent_id") for i in content if i in nodes and nodes[i].get("agent_id")}
                    | {w[0] for w in writers})

    cross = [{"source_node_id": a, "destination_tenant": b, "relationship": "LEAKED_INTO",
              "status": "unresolved_requires_authorized_remediation"}
             for a, r, b in edges if r == "LEAKED_INTO" and a in content]
    for f in cross:
        warnings.append(warn("CROSS_TENANT_LEAK", "a covered fact is marked LEAKED_INTO another tenant; "
                             "its copy there is not deleted by this operation", f["source_node_id"]))
    shared = []
    for did, d in sorted(derived.items()):
        if d["depends_on_other_users"]:
            shared.append({"node_id": did, "other_user_ids": d["depends_on_other_users"]})
            warnings.append(warn("SHARED_DERIVED_NODE", "also depends on data about another user; deleted "
                                 "under the derived-data rule", did))
    blocking = [w for w in warnings if w["severity"] == "blocking"]

    by_type = Counter(labels.values())
    by_edge = Counter(r for _, r, _ in edges)
    token = _token(ctx.tenant_id, user_id, labels, edges)
    plan = {
        "user_id": user_id, "tenant_id": ctx.tenant_id, "preview_id": f"prev_{token[:12]}",
        "preview_version": token, "max_depth": core["max_depth"],
        "candidates": [{"id": i, "type": labels[i], "category": CATEGORY[labels[i]]}
                       for i in node_ids if i in labels],
        "node_ids": node_ids,
        "edges": [{"src": a, "rel": r, "dst": b} for a, r, b in edges],
        "counts_by_node_type": dict(sorted(by_type.items())),
        "counts_by_edge_type": dict(sorted(by_edge.items())),
        "affected_agents": agents,
        "affected_memories": {"deleted": sorted(cand_memories), "kept_with_other_content": sorted(kept_memories)},
        "derived": {"summaries": sorted(i for i, d in derived.items() if d["label"] == "Summary"),
                    "inferences": sorted(i for i, d in derived.items() if d["label"] == "Inference"),
                    "actions": sorted(i for i, d in derived.items() if d["label"] == "Action")},
        "kept_sources": sorted(kept_sources),
        "shared_derived_nodes": shared,
        "cross_tenant_findings": cross,
        "warnings": warnings, "blocking": blocking, "can_execute": not blocking,
        "policy": POLICY,
    }
    return plan, core["user"]["display_name"]


# ---------- audit ----------
def audit_core(rec: Recorder, ctx: ToolContext, user_id: str, *, deleted_ids: list[str] | None = None,
               text_terms: list[str] | None = None, other_tenants: list[str] | None = None) -> dict:
    """Independent check of what is left. It walks upstream from every node toward the user's
    facts (the plan walks downstream from the facts), then scans the whole graph for orphans."""
    depth = depth_limit(ctx)
    user_present = bool(rec.read("MATCH (u:User {id: $uid}) RETURN u.id", {"uid": user_id}))
    facts = [r[0] for r in rec.read("MATCH (f:Fact)-[:ABOUT]->(:User {id: $uid}) RETURN f.id", {"uid": user_id})]
    rows = rec.read(
        f"MATCH p=(d)-[:{DERIVATION}*1..{depth + 1}]->(f:Fact)-[:ABOUT]->(:User {{id: $uid}}) "
        f"RETURN d.id, size(relationships(p)) LIMIT {ROW_CAP + 1}", {"uid": user_id})
    unresolved: list[dict] = []
    if len(rows) > ROW_CAP or any(n - 1 > depth for _, n in rows):
        unresolved.append(warn("TRUNCATED_TRAVERSAL", f"audit walk stopped at {depth} hops"))
    covered = sorted({*facts, *(d for d, n in rows[:ROW_CAP] if n - 1 <= depth)})
    paths = len(facts) + sum(1 for _, n in rows[:ROW_CAP] if n - 1 <= depth)

    residual = []
    if deleted_ids:
        residual = sorted(r[0] for r in rec.read("MATCH (n) WHERE n.id IN $ids RETURN n.id", {"ids": deleted_ids}))

    checks = [
        ("ORPHANED_DERIVED_NODE", "derived node has no parent belief",
         "MATCH (n) WHERE (n:Summary AND NOT (n)-[:SUMMARIZES]->()) OR (n:Inference AND NOT "
         "(n)-[:INFERRED_FROM]->()) OR (n:Action AND NOT (n)-[:BASED_ON]->()) RETURN n.id"),
        ("UNATTRIBUTED_FACT", "fact has no subject, so it cannot be tied to any user",
         "MATCH (f:Fact) WHERE NOT (f)-[:ABOUT]->(:User) RETURN f.id"),
        ("MISSING_SOURCE", "fact has no source", "MATCH (f:Fact) WHERE NOT (f)-[:DERIVED_FROM]->(:Source) RETURN f.id"),
        ("ORPHANED_SOURCE", "source is used by no fact",
         "MATCH (s:Source) WHERE NOT ((:Fact)-[:DERIVED_FROM]->(s)) RETURN s.id"),
        ("EMPTY_MEMORY", "memory container is empty", "MATCH (m:Memory) WHERE NOT (m)-[:CONTAINS]->() RETURN m.id"),
    ]
    for code, msg, q in checks:
        unresolved += [warn(code, msg, r[0]) for r in rec.read(q)]
    unresolved += [warn("TENANT_MISMATCH", "node tenant_id differs from this graph's tenant", r[0], "blocking")
                   for r in rec.read("MATCH (n) WHERE n.tenant_id IS NOT NULL AND n.tenant_id <> $t RETURN n.id",
                                     {"t": ctx.tenant_id})]
    for i, term in enumerate(text_terms or []):
        for nid, label in rec.read(
                "MATCH (n) WHERE n.text IS NOT NULL AND toLower(n.text) CONTAINS toLower($t) "
                "RETURN n.id, labels(n)[0]", {"t": term}):
            unresolved.append(warn("POSSIBLE_TEXT_MENTION", f"{label} text matches supplied identifier #{i + 1}; "
                                   "a text match is a heuristic, not proof", nid))

    checked = [ctx.graph_name_of(ctx.tenant_id)]
    cross = {"checked": False, "reason": "tenant_scope is limited to the home tenant"}
    if other_tenants:
        refs_from = deleted_ids or facts
        canon = graph_name_for(ctx.tenant_id)
        refs = [f"{canon}:{i}" for i in refs_from]
        found = []
        for t in other_tenants:
            checked.append(ctx.graph_name_of(t))
            r2 = Recorder(ctx.graph_for(t))
            hits = r2.read("MATCH (s:Source {type: 'cross_tenant_copy'}) WHERE s.url_or_session IN $refs "
                           "RETURN s.id", {"refs": refs}) if refs else []
            rec.cypher += r2.cypher
            rec.params += r2.params
            for (sid,) in hits:
                found.append({"tenant": t, "source_id": sid})
                unresolved.append(warn("CROSS_TENANT_COPY_REMAINS", f"a copy referencing erased data exists in "
                                       f"tenant {t}; remediation needs separate authorization", sid))
        cross = {"checked": True, "copies_found": found}

    passed = not covered and not user_present and not residual
    return {
        "graph_audit_passed": passed, "covered_paths_remaining": paths, "covered_nodes_remaining": covered,
        "user_node_present": user_present, "deleted_ids_still_present": residual,
        "unresolved_warnings": unresolved, "cross_tenant": cross,
        "scope": {"tenant_graphs_checked": checked, "external_systems_checked": False},
        "limitations": LIMITATIONS,
    }


def audit_forgotten(ctx: ToolContext, user_id: str, *, deleted_node_ids: list[str] | None = None,
                    text_terms: list[str] | None = None, tenant_scope: list[str] | None = None,
                    authorization: Authorization | None = None) -> dict:
    """Check the remaining graph for covered data about a user."""
    rec = Recorder(ctx.graph)
    check_id(user_id, "user_id")
    scope = tenant_scope or [ctx.tenant_id]
    if ctx.tenant_id not in scope:
        raise ToolError("INVALID_INPUT", "tenant_scope must include the home tenant")
    others = [t for t in scope if t != ctx.tenant_id]
    if others:
        require_authorization(authorization, scope, {"tenant_admin", "auditor"})
    for t in others:
        graph_name_for(t)
    terms = [t for t in (text_terms or []) if isinstance(t, str) and 2 <= len(t) <= 80]
    res = audit_core(rec, ctx, user_id, deleted_ids=deleted_node_ids, text_terms=terms, other_tenants=others)
    return build_result(rec, res, None, res["unresolved_warnings"])


# ---------- deletion ----------
def _delete_exact(rec: Recorder, ids: list[str]) -> int:
    """Delete exactly these nodes in one statement, or nothing. The statement re-checks, at
    execution time, that (1) every node still exists, (2) no outside node has gained a
    derivation/provenance edge into the set, and (3) no outside node was added to a doomed Memory."""
    rows = rec.write(
        """
        MATCH (n) WHERE n.id IN $ids
        WITH collect(n) AS ns WHERE size(ns) = $n
        OPTIONAL MATCH (x)-[:SUMMARIZES|INFERRED_FROM|BASED_ON|DERIVED_FROM|ABOUT]->(m)
          WHERE m.id IN $ids AND NOT x.id IN $ids
        WITH ns, count(x) AS stray1
        OPTIONAL MATCH (mm:Memory)-[:CONTAINS]->(y) WHERE mm.id IN $ids AND NOT y.id IN $ids
        WITH ns, stray1, count(y) AS stray2 WHERE stray1 = 0 AND stray2 = 0
        UNWIND ns AS d DETACH DELETE d
        RETURN count(d)
        """,
        {"ids": ids, "n": len(ids)})
    return rows[0][0] if rows else 0


def _write_receipt(ctx: ToolContext, receipt: dict) -> tuple[str | None, str | None]:
    d = ctx.resolved_receipt_dir()
    if not d:
        return None, None
    try:
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{receipt['job_id']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(receipt, fh, indent=2, sort_keys=True)
        return path, None
    except OSError as exc:
        return None, f"receipt file not written ({type(exc).__name__})"


def forget(ctx: ToolContext, user_id: str, dry_run: bool = True, *,
           authorization: Authorization | None = None, preview_version: str | None = None,
           reason: str | None = None) -> dict:
    """Preview (dry_run=True, never mutates) or perform (dry_run=False) erasure of a user's
    covered data in this tenant's graph."""
    check_id(user_id, "user_id")
    roles = {"data_subject", "tenant_admin", "auditor"} if dry_run else {"data_subject", "tenant_admin"}
    require_authorization(authorization, [ctx.tenant_id], roles, subject_user_id=user_id)
    rec = Recorder(ctx.graph)

    if dry_run:
        plan, _ = compute_plan(rec, ctx, user_id)
        keep = [*plan["node_ids"], *(m for m in plan["affected_memories"]["kept_with_other_content"])]
        result = {"dry_run": True, "mutated": False, "can_execute": plan["can_execute"], "preview": plan}
        return build_result(rec, result, fragment(rec, keep, strip_text=True), plan["warnings"])

    if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
        raise ToolError("INVALID_INPUT", "reason is required (at most 200 characters)")
    if not preview_version:
        raise ToolError("PREVIEW_REQUIRED", "run a dry-run first and pass its preview_version")
    requested_at = now_iso()
    plan, display_name = compute_plan(rec, ctx, user_id)
    if plan["blocking"]:
        raise ToolError("ERASURE_BLOCKED", "unresolved provenance problems; nothing was deleted",
                        {"blocking": plan["blocking"]})
    if plan["preview_version"] != preview_version:
        raise ToolError("PREVIEW_STALE", "the deletion set changed since the preview; review the new preview",
                        {"new_preview": plan})
    ids = plan["node_ids"]
    if _delete_exact(rec, ids) != len(ids):
        raise ToolError("DELETE_ABORTED", "the graph changed during deletion; nothing was deleted")

    warnings = [w for w in plan["warnings"] if w["severity"] != "blocking"]
    left = rec.read("MATCH (n) WHERE n.id IN $ids RETURN n.id", {"ids": ids})
    if left:
        warnings.append(warn("PARTIAL_FAILURE", f"{len(left)} node(s) still present after deletion", None, "blocking"))
    audit = audit_core(rec, ctx, user_id, deleted_ids=ids, text_terms=[display_name])
    receipt = {
        "job_id": new_id("erase"), "user_id": user_id, "tenant_id": ctx.tenant_id,
        "requested_at": requested_at, "completed_at": now_iso(), "reason": reason.strip(),
        "deleted_node_ids": ids, "deleted_edge_descriptors": plan["edges"],
        "counts_by_node_type": plan["counts_by_node_type"], "counts_by_edge_type": plan["counts_by_edge_type"],
        "affected_agents": plan["affected_agents"], "cross_tenant_findings": plan["cross_tenant_findings"],
        "preview_version": plan["preview_version"], "audit_result": audit, "warnings": warnings,
        "integrity": "none: this receipt is not signed or tamper-proof",
    }
    path, err = _write_receipt(ctx, receipt)
    if err:
        warnings.append(warn("RECEIPT_NOT_SAVED", err))
    result = {"dry_run": False, "mutated": True, "receipt": receipt, "receipt_path": path}
    graph = {"nodes": [], "edges": [], "removed_nodes": ids, "removed_edges": plan["edges"]}
    return build_result(rec, result, graph, warnings + audit["unresolved_warnings"])


# ---------- cross-tenant leaks ----------
def detect_leak(ctx: ToolContext, tenant_a: str, tenant_b: str, *, authorization: Authorization | None = None,
                user_id: str | None = None) -> dict:
    """Find data from tenant_a that has leaked into tenant_b. Read-only: the remediation is a
    preview that needs separate, explicit authorization and is never executed here."""
    if tenant_a != ctx.tenant_id:
        raise ToolError("TENANT_MISMATCH", "tenant_a must be the context's tenant")
    if tenant_a == tenant_b:
        raise ToolError("INVALID_INPUT", "tenant_a and tenant_b must differ")
    graph_name_for(tenant_b)
    require_authorization(authorization, [tenant_a, tenant_b], {"tenant_admin", "auditor"})
    if user_id is not None:
        check_id(user_id, "user_id")
    depth = depth_limit(ctx)
    ra, rb = Recorder(ctx.graph), Recorder(ctx.graph_for(tenant_b))
    prefix = f"{graph_name_for(tenant_a)}:"

    declared = {f: u for f, u in ra.read(
        "MATCH (f:Fact)-[:LEAKED_INTO]->(:Tenant {id: $b}) OPTIONAL MATCH (f)-[:ABOUT]->(u:User) RETURN f.id, u.id",
        {"b": tenant_b})}
    copies: dict[str, list[dict]] = {}
    for sid, ref in rb.read("MATCH (s:Source {type: 'cross_tenant_copy'}) WHERE s.url_or_session STARTS WITH $p "
                            "RETURN s.id, s.url_or_session", {"p": prefix}):
        copies.setdefault(ref[len(prefix):], []).append({"source_id": sid})
    for fact_id, items in copies.items():
        for it in items:
            rows = rb.read(
                f"MATCH (s:Source {{id: $sid}})<-[:DERIVED_FROM]-(f:Fact) OPTIONAL MATCH "
                f"(f)<-[:{DERIVATION}*1..{depth}]-(d) RETURN f.id, collect(DISTINCT d.id)", {"sid": it["source_id"]})
            it["copy_nodes"] = [{"node_id": f, "derived_node_ids": sorted(x for x in ds if x)} for f, ds in rows]
    undeclared_subjects = {}
    undeclared = [f for f in copies if f not in declared]
    if undeclared:
        for f, u in ra.read("MATCH (f:Fact) WHERE f.id IN $ids OPTIONAL MATCH (f)-[:ABOUT]->(u:User) RETURN f.id, u.id",
                            {"ids": undeclared}):
            undeclared_subjects[f] = u

    findings, warnings = [], []
    for fact_id in sorted({*declared, *copies}):
        is_declared = fact_id in declared
        subject = declared[fact_id] if is_declared else undeclared_subjects.get(fact_id)
        if user_id is not None and subject != user_id:
            continue
        found = copies.get(fact_id, [])
        status = "undeclared_copy" if not is_declared else ("copy_found" if found else "edge_only_no_copy_found")
        findings.append({"source_fact_id": fact_id, "source_tenant": tenant_a, "destination_tenant": tenant_b,
                         "subject_user_id": subject, "declared_via_leaked_into": is_declared,
                         "derives_from_requesting_user": (subject == user_id) if user_id else None,
                         "status": status, "copies": found})
        if status != "copy_found":
            warnings.append(warn(status.upper(), "leak edge and copy do not match; investigate", fact_id))
    prop = sorted({n["node_id"] for f in findings for c in f["copies"] for n in c["copy_nodes"]}
                  | {d for f in findings for c in f["copies"] for n in c["copy_nodes"] for d in n["derived_node_ids"]}
                  | {c["source_id"] for f in findings for c in f["copies"]})
    result = {"tenant_a": tenant_a, "tenant_b": tenant_b, "findings": findings,
              "remediation_preview": {"requires_explicit_authorization": True, "executed": False,
                                      "tenant": tenant_b, "proposed_node_ids": prop,
                                      "note": "Nothing in the other tenant was changed. Deleting these nodes needs a "
                                              "separate, explicitly authorized remediation step."}}
    rec = Recorder(ctx.graph)
    rec.cypher, rec.params = ra.cypher + rb.cypher, ra.params + rb.params
    return build_result(rec, result, {"nodes": [], "edges": []}, warnings)
