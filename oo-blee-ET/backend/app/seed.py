"""Synthetic seed data and loader. Synthetic only: no real personal information.

The dataset is plain Python data; the loader writes it to FalkorDB with parameterized
UNWIND/MERGE queries. Labels and relationship types come only from schema.py constants.
Timestamps are fixed (not "now") so every seed run is identical.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.schema import NODE_LABELS, check_pattern, ensure_schema
from app.tenants import TENANTS, graph_name_for, validate_graph_name

_BASE = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)


def ts(minutes: int) -> str:
    return (_BASE + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Edge:
    src_label: str
    src_id: str
    rel: str
    dst_label: str
    dst_id: str


@dataclass
class GraphData:
    tenant_id: str
    nodes: list[tuple[str, dict]] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    _label_of: dict[str, str] = field(default_factory=dict)

    def node(self, label: str, **props) -> None:
        if label not in NODE_LABELS:
            raise ValueError(f"unknown label {label}")
        if props["id"] in self._label_of:
            raise ValueError(f"duplicate node id {props['id']}")
        self._label_of[props["id"]] = label
        self.nodes.append((label, props))

    def link(self, src_id: str, rel: str, dst_id: str) -> None:
        s, d = self._label_of[src_id], self._label_of[dst_id]
        check_pattern(s, rel, d)
        self.edges.append(Edge(s, src_id, rel, d, dst_id))


def _acme() -> GraphData:
    g, t = GraphData("acme"), "acme"
    g.node("Tenant", id="acme", name="Acme")
    g.node("Tenant", id="globex", name="Globex")  # reference node: target of LEAKED_INTO
    for aid, role in (("agent_concierge", "concierge"), ("agent_analyst", "analyst"),
                      ("agent_auditor", "auditor")):
        g.node("Agent", id=aid, role=role)
    g.node("User", id="user_ravi", tenant_id=t, display_name="Ravi")
    g.node("User", id="user_priya", tenant_id=t, display_name="Priya")

    for sid, typ, sess, m in (
        ("src_chat_ravi_001", "chat_session", "session:sess_ravi_001", 0),
        ("src_chat_priya_001", "chat_session", "session:sess_priya_001", 5),
        ("src_meeting_team_001", "meeting_transcript", "session:sess_team_001", 10),
    ):
        g.node("Source", id=sid, type=typ, url_or_session=sess, created_at=ts(m))

    # id, tier, agent, session
    for mid, tier, agent, sess in (
        ("mem_concierge_ravi", "episodic", "agent_concierge", "sess_ravi_001"),
        ("mem_concierge_priya", "episodic", "agent_concierge", "sess_priya_001"),
        ("mem_concierge_team", "episodic", "agent_concierge", "sess_team_001"),
        ("mem_analyst_ravi_proc", "procedural", "agent_analyst", "sess_analyst_001"),
        ("mem_analyst_semantic", "semantic", "agent_analyst", "sess_analyst_001"),
        ("mem_analyst_actions", "procedural", "agent_analyst", "sess_analyst_001"),
    ):
        g.node("Memory", id=mid, tier=tier, agent_id=agent, session_id=sess, tenant_id=t)
        g.link(agent, "WROTE", mid)

    # id, text, tier, sensitivity, minute, agent, user, source, memory
    facts = [
        ("fact_ravi_city_pune", "Ravi lives in Pune.", "episodic", "low", 1,
         "agent_concierge", "user_ravi", "src_chat_ravi_001", "mem_concierge_ravi"),
        ("fact_ravi_city_bengaluru", "Ravi moved to Bengaluru.", "episodic", "low", 2,
         "agent_concierge", "user_ravi", "src_chat_ravi_001", "mem_concierge_ravi"),
        ("fact_ravi_trip_lisbon", "Ravi is planning a trip to Lisbon in March.", "episodic",
         "medium", 3, "agent_concierge", "user_ravi", "src_chat_ravi_001", "mem_concierge_ravi"),
        ("fact_ravi_vegetarian", "Ravi eats vegetarian food.", "episodic", "medium", 4,
         "agent_concierge", "user_ravi", "src_chat_ravi_001", "mem_concierge_ravi"),
        ("fact_ravi_digest_friday", "Send Ravi the weekly digest on Fridays at 9 am.",
         "procedural", "low", 20, "agent_analyst", "user_ravi", "src_chat_ravi_001",
         "mem_analyst_ravi_proc"),
        ("fact_ravi_owns_onboarding", "Ravi owns the Q4 onboarding checklist.", "episodic",
         "low", 11, "agent_concierge", "user_ravi", "src_meeting_team_001",
         "mem_concierge_team"),
        ("fact_priya_role_finance", "Priya works on the finance team.", "episodic", "low", 6,
         "agent_concierge", "user_priya", "src_chat_priya_001", "mem_concierge_priya"),
        ("fact_priya_email_pref", "Priya prefers email over phone calls.", "episodic", "low",
         7, "agent_concierge", "user_priya", "src_chat_priya_001", "mem_concierge_priya"),
        ("fact_priya_owns_budget", "Priya owns the Q4 budget review.", "episodic", "low", 12,
         "agent_concierge", "user_priya", "src_meeting_team_001", "mem_concierge_team"),
    ]
    for fid, text, tier, sens, m, agent, user, src, mem in facts:
        g.node("Fact", id=fid, text=text, tier=tier, sensitivity=sens, created_at=ts(m),
               agent_id=agent, tenant_id=t)
        g.link(fid, "DERIVED_FROM", src)
        g.link(fid, "ABOUT", user)
        g.link(mem, "CONTAINS", fid)
    g.link("fact_ravi_city_pune", "SUPERSEDED_BY", "fact_ravi_city_bengaluru")
    g.link("fact_ravi_trip_lisbon", "LEAKED_INTO", "globex")

    # id, text, minute, parents (facts or summaries)
    summaries = [
        ("sum_ravi_profile",
         "Ravi is based in Bengaluru, is planning a Lisbon trip, and eats vegetarian food.", 30,
         ["fact_ravi_city_bengaluru", "fact_ravi_trip_lisbon", "fact_ravi_vegetarian"]),
        ("sum_ravi_overview", "Ravi is a Bengaluru-based vegetarian who travels.", 31,
         ["sum_ravi_profile"]),
        ("sum_priya_profile", "Priya is on the finance team and prefers email.", 32,
         ["fact_priya_role_finance", "fact_priya_email_pref"]),
        # Mixed: depends on facts about two different users.
        ("sum_team_q4", "Q4 planning: Ravi owns onboarding, Priya owns the budget review.", 33,
         ["fact_ravi_owns_onboarding", "fact_priya_owns_budget"]),
    ]
    for sid, text, m, parents in summaries:
        g.node("Summary", id=sid, text=text, tier="semantic", created_at=ts(m),
               agent_id="agent_analyst", tenant_id=t)
        g.link("mem_analyst_semantic", "CONTAINS", sid)
        for p in parents:
            g.link(sid, "SUMMARIZES", p)

    inferences = [
        ("inf_ravi_lisbon_veg", "Ravi will want vegetarian restaurant suggestions in Lisbon.",
         0.82, 40, ["fact_ravi_trip_lisbon", "fact_ravi_vegetarian"]),
        ("inf_ravi_traveler", "Ravi travels often.", 0.6, 41, ["sum_ravi_profile"]),
        ("inf_priya_email", "Priya should receive updates by email.", 0.9, 42,
         ["fact_priya_email_pref"]),
    ]
    for iid, text, conf, m, parents in inferences:
        g.node("Inference", id=iid, text=text, tier="semantic", confidence=conf,
               created_at=ts(m), agent_id="agent_analyst", tenant_id=t)
        g.link("mem_analyst_semantic", "CONTAINS", iid)
        for p in parents:
            g.link(iid, "INFERRED_FROM", p)

    for aid, typ, m, parent in (
        ("act_ravi_suggest_restaurants", "suggest_restaurants", 50, "inf_ravi_lisbon_veg"),
        ("act_ravi_travel_newsletter", "send_travel_newsletter", 51, "inf_ravi_traveler"),
        ("act_priya_send_update", "send_email_update", 52, "inf_priya_email"),
    ):
        g.node("Action", id=aid, type=typ, status="proposed", created_at=ts(m),
               agent_id="agent_analyst", tenant_id=t)
        g.link("mem_analyst_actions", "CONTAINS", aid)
        g.link(aid, "BASED_ON", parent)
    return g


def _globex() -> GraphData:
    """Holds the leaked copy of one Acme fact. It has no ABOUT edge: edges cannot cross
    graphs, so its subject is resolvable only through the Source reference."""
    g, t = GraphData("globex"), "globex"
    g.node("Tenant", id="globex", name="Globex")
    g.node("Agent", id="agent_analyst", role="analyst")
    g.node("Memory", id="mem_globex_analyst_copy", tier="episodic", agent_id="agent_analyst",
           session_id="sess_analyst_002", tenant_id=t)
    g.link("agent_analyst", "WROTE", "mem_globex_analyst_copy")
    g.node("Source", id="src_xtenant_copy_lisbon", type="cross_tenant_copy",
           url_or_session="tenant_acme:fact_ravi_trip_lisbon", created_at=ts(60))
    g.node("Fact", id="fact_globex_copy_lisbon",
           text="Ravi is planning a trip to Lisbon in March.", tier="episodic",
           sensitivity="medium", created_at=ts(61), agent_id="agent_analyst", tenant_id=t)
    g.link("fact_globex_copy_lisbon", "DERIVED_FROM", "src_xtenant_copy_lisbon")
    g.link("mem_globex_analyst_copy", "CONTAINS", "fact_globex_copy_lisbon")
    return g


def build_dataset() -> dict[str, GraphData]:
    return {"acme": _acme(), "globex": _globex()}


def load_graph(graph, data: GraphData) -> None:
    ensure_schema(graph)
    by_label: dict[str, list[dict]] = defaultdict(list)
    for label, props in data.nodes:
        by_label[label].append(props)
    for label, rows in by_label.items():  # label comes from the NODE_LABELS allowlist
        graph.query(f"UNWIND $rows AS r MERGE (n:{label} {{id: r.id}}) SET n += r", {"rows": rows})

    by_pattern: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for e in data.edges:
        check_pattern(e.src_label, e.rel, e.dst_label)
        by_pattern[(e.src_label, e.rel, e.dst_label)].append({"s": e.src_id, "d": e.dst_id})
    for (s, rel, d), rows in by_pattern.items():
        graph.query(
            f"UNWIND $rows AS r MATCH (a:{s} {{id: r.s}}), (b:{d} {{id: r.d}}) "
            f"MERGE (a)-[:{rel}]->(b)",
            {"rows": rows},
        )


def graph_counts(graph) -> dict:
    nodes = graph.ro_query("MATCH (n) RETURN labels(n)[0], count(n)").result_set
    edges = graph.ro_query("MATCH ()-[r]->() RETURN type(r), count(r)").result_set
    return {"nodes": dict(nodes), "edges": dict(edges)}


def seed_all(client, *, reset: bool = False, graph_names: dict[str, str] | None = None) -> dict:
    """Seed every tenant graph. With reset=True, drops and recreates the named graphs first.
    graph_names lets tests use throwaway graphs instead of the real tenant graphs."""
    names = graph_names or {t: graph_name_for(t) for t in TENANTS}
    out = {}
    for tenant_id, data in build_dataset().items():
        name = validate_graph_name(names[tenant_id])
        if reset and name in client.list_graphs():
            client.select_graph(name).delete()
        graph = client.select_graph(name)
        load_graph(graph, data)
        out[tenant_id] = {"graph": name, **graph_counts(graph)}
    return out
