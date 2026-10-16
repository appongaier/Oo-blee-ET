"""Write tools: remember, summarize, infer, act, mark_superseded.

Each write is one Cypher statement, so it is applied all-or-nothing by the server. Derived
nodes are created with a guard: if any parent is missing, wrong-typed, or in another tenant,
the statement matches nothing and creates nothing.
"""
from __future__ import annotations

from app.tools.common import (ACTION_TYPE_RE, Recorder, ToolContext, ToolError, build_result,
                              check_id, check_id_list, check_sensitivity, check_session,
                              check_text, check_tier, fragment, get_agent, get_user, new_id,
                              now_iso, warn)

# Roles by capability. The Auditor never writes.
_WRITERS = {"concierge", "analyst"}
_ANALYSTS = {"analyst"}


def _find_memory(rec: Recorder, ctx: ToolContext, agent_id: str, tier: str, session_id: str) -> str:
    rows = rec.read(
        "MATCH (m:Memory {agent_id: $a, tier: $t, session_id: $s, tenant_id: $tid}) RETURN m.id LIMIT 1",
        {"a": agent_id, "t": tier, "s": session_id, "tid": ctx.tenant_id},
    )
    return rows[0][0] if rows else new_id("mem")


def remember(ctx: ToolContext, user_id: str, text: str, tier: str, agent_id: str, *,
             sensitivity: str = "low", session_id: str | None = None) -> dict:
    """Create a Fact about a user with its Source, Memory container and provenance edges."""
    rec = Recorder(ctx.graph)
    text, tier, sensitivity = check_text(text), check_tier(tier), check_sensitivity(sensitivity)
    session_id = check_session(session_id)
    get_agent(rec, agent_id, _WRITERS)
    get_user(rec, ctx, user_id)

    ref = f"session:{session_id}"
    rows = rec.read("MATCH (s:Source {url_or_session: $ref}) RETURN s.id LIMIT 1", {"ref": ref})
    source_id = rows[0][0] if rows else new_id("src")
    memory_id = _find_memory(rec, ctx, agent_id, tier, session_id)
    fact_id, now = new_id("fact"), now_iso()

    rows = rec.write(
        """
        MATCH (u:User {id: $user_id, tenant_id: $tenant_id}), (a:Agent {id: $agent_id})
        MERGE (s:Source {id: $source_id})
          ON CREATE SET s.type = 'chat_session', s.url_or_session = $ref, s.created_at = $now
        MERGE (m:Memory {id: $memory_id})
          ON CREATE SET m.tier = $tier, m.agent_id = $agent_id, m.session_id = $session_id,
                        m.tenant_id = $tenant_id
        MERGE (a)-[:WROTE]->(m)
        CREATE (f:Fact {id: $fact_id, text: $text, tier: $tier, sensitivity: $sensitivity,
                        created_at: $now, agent_id: $agent_id, tenant_id: $tenant_id})
        CREATE (f)-[:DERIVED_FROM]->(s), (f)-[:ABOUT]->(u), (m)-[:CONTAINS]->(f)
        RETURN f.id
        """,
        {"user_id": user_id, "tenant_id": ctx.tenant_id, "agent_id": agent_id,
         "source_id": source_id, "ref": ref, "now": now, "memory_id": memory_id, "tier": tier,
         "session_id": session_id, "fact_id": fact_id, "text": text, "sensitivity": sensitivity},
    )
    if not rows:
        raise ToolError("WRITE_REJECTED", "user or agent changed during the write; nothing was created")
    result = {"fact_id": fact_id, "user_id": user_id, "agent_id": agent_id, "tenant_id": ctx.tenant_id,
              "tier": tier, "memory_id": memory_id, "source_id": source_id, "created_at": now,
              "edges_created": [
                  {"src": fact_id, "rel": "DERIVED_FROM", "dst": source_id},
                  {"src": fact_id, "rel": "ABOUT", "dst": user_id},
                  {"src": memory_id, "rel": "CONTAINS", "dst": fact_id}]}
    g = fragment(rec, [fact_id, source_id, memory_id, user_id, agent_id])
    return build_result(rec, result, g)


def _derive(ctx: ToolContext, *, agent_id: str, parent_ids: list[str], parent_labels: tuple[str, ...],
            label: str, rel: str, memory_tier: str, props: dict, session_id: str | None) -> tuple:
    """Shared guarded write for Summary / Inference / Action. Labels and relationship types
    come from fixed constants in the three public tools below, never from caller input."""
    rec = Recorder(ctx.graph)
    session_id = check_session(session_id)
    get_agent(rec, agent_id, _ANALYSTS)

    rows = rec.read(
        "MATCH (p) WHERE p.id IN $ids RETURN p.id, labels(p)[0], p.tenant_id, "
        "size([(p)-[:SUPERSEDED_BY]->() | 1])",
        {"ids": parent_ids},
    )
    found = {r[0]: r for r in rows}
    missing = [i for i in parent_ids if i not in found]
    if missing:
        raise ToolError("PARENT_NOT_FOUND", f"not found in tenant {ctx.tenant_id}: {missing}")
    bad = [i for i, r in found.items() if r[1] not in parent_labels]
    if bad:
        raise ToolError("PARENT_TYPE", f"{bad} must be one of {list(parent_labels)}")
    wrong = [i for i, r in found.items() if r[2] != ctx.tenant_id]
    if wrong:
        raise ToolError("TENANT_MISMATCH", f"nodes {wrong} do not belong to tenant {ctx.tenant_id}")
    warnings = [warn("SUPERSEDED_PARENT", "derives from a superseded fact; it remains covered by erasure", i)
                for i, r in found.items() if r[3]]

    memory_id = _find_memory(rec, ctx, agent_id, memory_tier, session_id)
    node_id = new_id({"Summary": "sum", "Inference": "inf", "Action": "act"}[label])
    node_props = {"id": node_id, "created_at": now_iso(), "agent_id": agent_id,
                  "tenant_id": ctx.tenant_id, **props}
    pred = " OR ".join(f"p:{l}" for l in parent_labels)
    rows = rec.write(
        f"""
        MATCH (a:Agent {{id: $agent_id}})
        MATCH (p) WHERE p.id IN $ids AND ({pred}) AND p.tenant_id = $tenant_id
        WITH a, collect(p) AS parents WHERE size(parents) = $n
        MERGE (m:Memory {{id: $memory_id}})
          ON CREATE SET m.tier = $tier, m.agent_id = $agent_id, m.session_id = $session_id,
                        m.tenant_id = $tenant_id
        MERGE (a)-[:WROTE]->(m)
        CREATE (c:{label}) SET c += $props
        CREATE (m)-[:CONTAINS]->(c)
        WITH c, parents UNWIND parents AS p
        CREATE (c)-[:{rel}]->(p)
        RETURN c.id, collect(p.id)
        """,
        {"agent_id": agent_id, "ids": parent_ids, "tenant_id": ctx.tenant_id, "n": len(parent_ids),
         "memory_id": memory_id, "tier": memory_tier, "session_id": session_id, "props": node_props},
    )
    if not rows:
        raise ToolError("PARENTS_CHANGED", "a parent changed during the write; nothing was created")
    result = {f"{label.lower()}_id": node_id, "agent_id": agent_id, "tenant_id": ctx.tenant_id,
              "memory_id": memory_id, "parent_ids": sorted(parent_ids), "created_at": node_props["created_at"],
              "edges_created": [{"src": node_id, "rel": rel, "dst": p} for p in sorted(parent_ids)]
                               + [{"src": memory_id, "rel": "CONTAINS", "dst": node_id}]}
    g = fragment(rec, [node_id, memory_id, agent_id, *parent_ids])
    return rec, result, g, warnings


def summarize(ctx: ToolContext, agent_id: str, fact_ids: list[str], text: str, *,
              session_id: str | None = None) -> dict:
    """Create a Summary of facts (or of earlier summaries) with SUMMARIZES edges.
    `text` is supplied by the caller (the Analyst); this tool never writes content itself."""
    ids = check_id_list(fact_ids, "fact_ids")
    text = check_text(text)
    rec, result, g, w = _derive(
        ctx, agent_id=agent_id, parent_ids=ids, parent_labels=("Fact", "Summary"), label="Summary",
        rel="SUMMARIZES", memory_tier="semantic", props={"text": text, "tier": "semantic"},
        session_id=session_id)
    return build_result(rec, result, g, w)


def infer(ctx: ToolContext, agent_id: str, fact_ids: list[str], text: str, *,
          confidence: float, session_id: str | None = None) -> dict:
    """Create an Inference with INFERRED_FROM edges to every source fact (or summary)."""
    ids = check_id_list(fact_ids, "fact_ids")
    text = check_text(text)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ToolError("INVALID_INPUT", "confidence must be a number between 0 and 1")
    rec, result, g, w = _derive(
        ctx, agent_id=agent_id, parent_ids=ids, parent_labels=("Fact", "Summary"), label="Inference",
        rel="INFERRED_FROM", memory_tier="semantic",
        props={"text": text, "tier": "semantic", "confidence": float(confidence)}, session_id=session_id)
    return build_result(rec, result, g, w)


def act(ctx: ToolContext, agent_id: str, inference_id: str | list[str], action_type: str, *,
        session_id: str | None = None) -> dict:
    """Create a proposed Action based on one or more inferences. Nothing is ever executed."""
    ids = check_id_list([inference_id] if isinstance(inference_id, str) else inference_id, "inference_id")
    if not isinstance(action_type, str) or not ACTION_TYPE_RE.fullmatch(action_type):
        raise ToolError("INVALID_INPUT", f"action_type must match {ACTION_TYPE_RE.pattern}")
    rec, result, g, w = _derive(
        ctx, agent_id=agent_id, parent_ids=ids, parent_labels=("Inference",), label="Action",
        rel="BASED_ON", memory_tier="procedural",
        props={"type": action_type, "status": "proposed"}, session_id=session_id)
    w.append(warn("NOT_EXECUTED", "synthetic demo: actions are recorded as proposed and never executed"))
    return build_result(rec, result, g, w)


def mark_superseded(ctx: ToolContext, old_fact_id: str, new_fact_id: str) -> dict:
    """Record that a newer fact replaces an older one. The old fact is kept as history and
    stays fully covered by erasure."""
    rec = Recorder(ctx.graph)
    old, new = check_id(old_fact_id, "old_fact_id"), check_id(new_fact_id, "new_fact_id")
    if old == new:
        raise ToolError("INVALID_INPUT", "a fact cannot supersede itself")
    rows = rec.read(
        "MATCH (f:Fact) WHERE f.id IN $ids RETURN f.id, f.tenant_id, [(f)-[:ABOUT]->(u) | u.id], "
        "size([(f)-[:SUPERSEDED_BY]->() | 1]), f.created_at",
        {"ids": [old, new]},
    )
    found = {r[0]: r for r in rows}
    for fid in (old, new):
        if fid not in found:
            raise ToolError("FACT_NOT_FOUND", f"fact {fid} not found in tenant {ctx.tenant_id}")
        if found[fid][1] != ctx.tenant_id:
            raise ToolError("TENANT_MISMATCH", f"fact {fid} does not belong to tenant {ctx.tenant_id}")
    subj_old, subj_new = found[old][2], found[new][2]
    if len(subj_old) != 1 or subj_old != subj_new:
        raise ToolError("SUBJECT_MISMATCH", "both facts must be about the same single user")
    if found[old][3]:
        raise ToolError("ALREADY_SUPERSEDED", f"fact {old} already has a successor")
    cyc = rec.read("MATCH (n:Fact {id: $new})-[:SUPERSEDED_BY*1..20]->(o:Fact {id: $old}) RETURN n.id LIMIT 1",
                   {"new": new, "old": old})
    if cyc:
        raise ToolError("CYCLE", "this would create a supersession cycle")
    rec.write("MATCH (o:Fact {id: $old}), (n:Fact {id: $new}) MERGE (o)-[:SUPERSEDED_BY]->(n) RETURN o.id",
              {"old": old, "new": new})
    warnings = [warn("COVERED_BY_ERASURE",
                     "the superseded fact is history, not an exemption: it stays covered by erasure", old)]
    if found[new][4] < found[old][4]:
        warnings.append(warn("NEWER_IS_OLDER", "the replacing fact has an earlier created_at", new))
    result = {"old_fact_id": old, "new_fact_id": new, "subject_user_id": subj_old[0],
              "edge": {"src": old, "rel": "SUPERSEDED_BY", "dst": new}, "old_fact_deleted": False}
    return build_result(rec, result, fragment(rec, [old, new, subj_old[0]]), warnings)
