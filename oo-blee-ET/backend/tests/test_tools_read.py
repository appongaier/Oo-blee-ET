from collections import defaultdict

import pytest

from app.schema import DERIVATION_RELS
from app.seed import build_dataset
from app.tools import ToolContext, ToolError, explain_belief, find_derived, trace_provenance
from tests.conftest import snapshot

pytestmark = pytest.mark.integration


def downstream_expected(start: str) -> set[str]:
    g = build_dataset()["acme"]
    kids = defaultdict(set)
    for e in g.edges:
        if e.rel in DERIVATION_RELS:
            kids[e.dst_id].add(e.src_id)
    seen, frontier = set(), {start}
    while frontier:
        frontier = set().union(*(kids[n] for n in frontier)) - seen
        seen |= frontier
    return seen


def test_trace_upstream_reaches_source_and_subject_with_directions(ctx):
    out = trace_provenance(ctx, "act_ravi_suggest_restaurants")
    up = out["result"]["upstream"]
    assert out["result"]["complete"] and not out["warnings"]
    assert set(up["node_ids"]) == {"inf_ravi_lisbon_veg", "fact_ravi_trip_lisbon", "fact_ravi_vegetarian",
                                   "src_chat_ravi_001", "user_ravi"}
    longest = max(up["paths"], key=lambda p: len(p["edges"]))
    assert [n["type"] for n in longest["nodes"]][:3] == ["Action", "Inference", "Fact"]
    assert longest["edges"][0] == {"src": "act_ravi_suggest_restaurants", "rel": "BASED_ON", "dst": "inf_ravi_lisbon_veg"}


def test_trace_downstream_matches_dataset(ctx):
    out = trace_provenance(ctx, "fact_ravi_trip_lisbon")
    assert set(out["result"]["downstream"]["node_ids"]) == downstream_expected("fact_ravi_trip_lisbon")
    p = out["result"]["downstream"]["paths"][0]
    assert p["edges"][0]["dst"] == "fact_ravi_trip_lisbon"      # descendants point at their parent


def test_trace_supports_source_and_user_nodes(ctx):
    src = trace_provenance(ctx, "src_meeting_team_001")["result"]["downstream"]["node_ids"]
    assert {"fact_ravi_owns_onboarding", "fact_priya_owns_budget", "sum_team_q4"} <= set(src)
    usr = trace_provenance(ctx, "user_priya")["result"]["downstream"]["node_ids"]
    assert {"fact_priya_email_pref", "inf_priya_email", "act_priya_send_update"} <= set(usr)


def test_trace_rejects_unsupported_and_unknown_nodes(ctx):
    for nid, code in (("mem_concierge_ravi", "UNSUPPORTED_NODE_TYPE"), ("nope", "NODE_NOT_FOUND")):
        with pytest.raises(ToolError) as e:
            trace_provenance(ctx, nid)
        assert e.value.code == code


def test_truncated_traversal_is_distinguished_from_complete(ctx):
    ctx.max_depth = 1
    out = trace_provenance(ctx, "act_ravi_suggest_restaurants")
    assert out["result"]["upstream"]["truncated"] and not out["result"]["complete"]
    assert any(w["code"] == "TRUNCATED_TRAVERSAL" for w in out["warnings"])
    ctx.max_depth = 8
    assert trace_provenance(ctx, "act_ravi_suggest_restaurants")["result"]["complete"]


def test_missing_provenance_is_reported_not_assumed(ctx):
    g = ctx.graph
    g.query("CREATE (:Summary {id:'sum_orphan', text:'floating', tier:'semantic', created_at:'2026-10-01T00:00:00Z', "
            "agent_id:'agent_analyst', tenant_id:'acme'})")
    codes = {w["code"] for w in trace_provenance(ctx, "sum_orphan")["warnings"]}
    assert {"MISSING_PARENT", "MISSING_CONTAINER"} <= codes
    ex = explain_belief(ctx, "sum_orphan")["result"]
    assert ex["supported"] is False and ex["explanation"].startswith("Evidence unavailable")


def test_explain_belief_for_a_shared_summary(ctx):
    out = explain_belief(ctx, "sum_team_q4")
    r = out["result"]
    assert r["supported"] and r["complete"] and not out["warnings"]
    assert {p["id"] for p in r["direct_parents"]} == {"fact_ravi_owns_onboarding", "fact_priya_owns_budget"}
    assert r["sources"] == ["src_meeting_team_001"] and r["subjects"] == ["user_priya", "user_ravi"]
    assert r["agents"] == ["agent_analyst", "agent_concierge"]
    assert {"mem_analyst_semantic", "mem_concierge_team"} <= {m["memory_id"] for m in r["memories"]}
    assert out["cypher"] and len(out["cypher"]) == len(out["params"])


def test_find_derived_matches_the_dataset_and_excludes_others(ctx):
    out = find_derived(ctx, "user_ravi")
    r = out["result"]
    derived = {d["id"] for d in r["derived"]}
    facts = {s["id"] for s in r["user_originated"]}
    assert derived == downstream_expected("fact_ravi_city_pune") | downstream_expected("fact_ravi_trip_lisbon") \
        | downstream_expected("fact_ravi_vegetarian") | downstream_expected("fact_ravi_owns_onboarding") \
        | downstream_expected("fact_ravi_city_bengaluru") | downstream_expected("fact_ravi_digest_friday")
    assert facts == {"fact_ravi_city_pune", "fact_ravi_city_bengaluru", "fact_ravi_trip_lisbon",
                     "fact_ravi_vegetarian", "fact_ravi_digest_friday", "fact_ravi_owns_onboarding"}
    assert not ({"sum_priya_profile", "inf_priya_email", "act_priya_send_update", "fact_priya_email_pref"}
                & (derived | facts))
    by_id = {d["id"]: d for d in r["derived"]}
    assert by_id["sum_team_q4"]["depends_on_other_users"] == ["user_priya"]
    assert by_id["sum_ravi_profile"]["depends_on_other_users"] == []
    assert {s["id"] for s in r["user_originated"] if s["superseded"]} == {"fact_ravi_city_pune"}
    assert r["complete"] and not out["warnings"]


def test_find_derived_is_read_only_and_tenant_scoped(ctx):
    before = snapshot(ctx.client, "acme")
    find_derived(ctx, "user_ravi")
    assert snapshot(ctx.client, "acme") == before
    globex = ToolContext(ctx.client, "globex", ctx.graph_names, receipt_dir="")
    with pytest.raises(ToolError) as e:
        find_derived(globex, "user_ravi")
    assert e.value.code == "USER_NOT_FOUND"


def test_find_derived_follows_multiple_levels_and_flags_truncation(ctx):
    ctx.max_depth = 1
    out = find_derived(ctx, "user_ravi")
    assert out["result"]["truncated"] and not out["result"]["complete"]
    assert any(w["code"] == "TRUNCATED_TRAVERSAL" and w["severity"] == "blocking" for w in out["warnings"])


def test_find_derived_reports_unattributed_facts(ctx):
    ctx.graph.query("CREATE (:Fact {id:'fact_stray', text:'no subject', tier:'episodic', sensitivity:'low', "
                    "created_at:'2026-10-01T00:00:00Z', agent_id:'agent_concierge', tenant_id:'acme'})")
    out = find_derived(ctx, "user_priya")
    assert any(w["code"] == "UNATTRIBUTED_FACT" and w["node_id"] == "fact_stray" for w in out["warnings"])
    assert "fact_stray" not in out["result"]["node_ids"]
