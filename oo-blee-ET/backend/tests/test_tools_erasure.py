import json
import os

import pytest

from app.tools import (Authorization, ToolContext, ToolError, audit_forgotten, detect_leak, find_derived,
                       forget, remember)
from app.tools.common import Recorder
from app.tools.erasure import _delete_exact
from app.seed import build_dataset
from tests.conftest import expected_erasure, snapshot

pytestmark = pytest.mark.integration
RAVI = Authorization("user_ravi", "data_subject", ("acme",))
PRIYA = Authorization("user_priya", "data_subject", ("acme",))
ADMIN = Authorization("admin_1", "tenant_admin", ("acme", "globex"))
AUDITOR = Authorization("agent_auditor", "auditor", ("acme", "globex"))


def preview(ctx, user="user_ravi", auth=RAVI):
    return forget(ctx, user, True, authorization=auth)["result"]["preview"]


def erase(ctx, user="user_ravi", auth=RAVI, version=None):
    version = version or preview(ctx, user, auth)["preview_version"]
    return forget(ctx, user, False, authorization=auth, preview_version=version, reason="user request")


# ---------- dry run ----------
def test_dry_run_never_mutates_and_matches_the_policy(ctx):
    before = snapshot(ctx.client, "acme")
    out = forget(ctx, "user_ravi", True, authorization=RAVI)
    assert snapshot(ctx.client, "acme") == before
    assert not any(w in q.upper() for q in out["cypher"] for w in ("CREATE ", "MERGE ", "DELETE ", " SET "))
    p = out["result"]["preview"]
    assert out["result"]["mutated"] is False and out["result"]["can_execute"]
    assert set(p["node_ids"]) == expected_erasure("user_ravi")
    assert p["counts_by_node_type"] == {"Action": 2, "Fact": 6, "Inference": 2, "Memory": 2, "Source": 1,
                                        "Summary": 3, "User": 1}
    assert p["kept_sources"] == ["src_meeting_team_001"]
    assert p["affected_memories"]["deleted"] == ["mem_analyst_ravi_proc", "mem_concierge_ravi"]
    assert "mem_concierge_team" in p["affected_memories"]["kept_with_other_content"]
    assert p["affected_agents"] == ["agent_analyst", "agent_concierge"]
    assert p["derived"]["summaries"] == ["sum_ravi_overview", "sum_ravi_profile", "sum_team_q4"]
    assert p["shared_derived_nodes"] == [{"node_id": "sum_team_q4", "other_user_ids": ["user_priya"]}]
    assert [f["destination_tenant"] for f in p["cross_tenant_findings"]] == ["globex"]
    assert sum(p["counts_by_edge_type"].values()) == len(p["edges"])
    assert not any("text" in n for n in out["graph"]["nodes"])
    assert preview(ctx)["preview_version"] == p["preview_version"]       # deterministic


def test_nothing_independent_is_in_the_deletion_set(ctx):
    ids = set(preview(ctx)["node_ids"])
    assert not ids & {"user_priya", "fact_priya_email_pref", "fact_priya_role_finance", "fact_priya_owns_budget",
                      "sum_priya_profile", "inf_priya_email", "act_priya_send_update", "src_chat_priya_001",
                      "src_meeting_team_001", "agent_concierge", "agent_analyst", "agent_auditor", "acme", "globex"}


# ---------- authorization ----------
@pytest.mark.parametrize("auth,dry,code", [
    (None, True, "UNAUTHORIZED"),
    (PRIYA, True, "FORBIDDEN_SUBJECT"),
    (Authorization("user_ravi", "data_subject", ("globex",)), True, "FORBIDDEN_TENANT"),
    (Authorization("x", "superuser", ("acme",)), True, "FORBIDDEN_ROLE"),
    (AUDITOR, False, "FORBIDDEN_ROLE"),
    (PRIYA, False, "FORBIDDEN_SUBJECT"),
])
def test_authorization_is_enforced(ctx, auth, dry, code):
    before = snapshot(ctx.client, "acme")
    with pytest.raises(ToolError) as e:
        forget(ctx, "user_ravi", dry, authorization=auth, preview_version="x", reason="r")
    assert e.value.code == code
    assert snapshot(ctx.client, "acme") == before


def test_auditor_may_preview_and_admin_may_erase(ctx):
    assert preview(ctx, auth=AUDITOR)["can_execute"]
    out = erase(ctx, auth=ADMIN)
    assert out["result"]["receipt"]["audit_result"]["graph_audit_passed"]


# ---------- confirmed erasure ----------
def test_confirmed_erasure_deletes_exactly_the_plan_and_preserves_everyone_else(ctx):
    before = snapshot(ctx.client, "acme")
    globex_before = snapshot(ctx.client, "globex")
    plan = preview(ctx)
    out = erase(ctx)
    after = snapshot(ctx.client, "acme")
    gone = set(plan["node_ids"])
    assert set(before["nodes"]) - set(after["nodes"]) == gone == expected_erasure("user_ravi")
    assert {k: v for k, v in before["nodes"].items() if k not in gone} == after["nodes"]   # survivors untouched
    assert after["edges"] == [e for e in before["edges"] if e[0] not in gone and e[2] not in gone]
    assert snapshot(ctx.client, "globex") == globex_before

    r = out["result"]["receipt"]
    assert set(r) >= {"job_id", "user_id", "tenant_id", "requested_at", "completed_at", "reason",
                      "deleted_node_ids", "deleted_edge_descriptors", "counts_by_node_type",
                      "counts_by_edge_type", "affected_agents", "cross_tenant_findings", "preview_version",
                      "audit_result", "warnings"}
    assert r["deleted_node_ids"] == plan["node_ids"] and r["preview_version"] == plan["preview_version"]
    assert r["deleted_edge_descriptors"] == plan["edges"]
    assert r["audit_result"]["graph_audit_passed"] and r["audit_result"]["covered_paths_remaining"] == 0
    assert r["audit_result"]["unresolved_warnings"] == []
    assert r["audit_result"]["scope"]["external_systems_checked"] is False
    assert [f["status"] for f in r["cross_tenant_findings"]] == ["unresolved_requires_authorized_remediation"]
    assert "not signed or tamper-proof" in r["integrity"]


def test_receipt_contains_no_erased_content_and_is_saved(ctx):
    texts = [t for _, p in build_dataset()["acme"].nodes if "text" in p
             and p["id"] in expected_erasure("user_ravi") for t in [p["text"]]]
    out = erase(ctx)
    blob = json.dumps(out["result"]["receipt"])
    assert texts and not any(t in blob for t in texts)
    assert "Ravi" not in blob
    path = out["result"]["receipt_path"]
    assert os.path.exists(path) and json.load(open(path)) == out["result"]["receipt"]


def test_erasing_priya_leaves_ravi_intact(ctx):
    out = erase(ctx, "user_priya", PRIYA)
    assert set(out["result"]["receipt"]["deleted_node_ids"]) == expected_erasure("user_priya")
    left = {s["id"] for s in find_derived(ctx, "user_ravi")["result"]["user_originated"]}
    assert "fact_ravi_trip_lisbon" in left and len(left) == 6


def test_erasing_twice_fails_cleanly(ctx):
    erase(ctx)
    with pytest.raises(ToolError) as e:
        forget(ctx, "user_ravi", True, authorization=RAVI)
    assert e.value.code == "USER_NOT_FOUND"


@pytest.mark.parametrize("kwargs,code", [
    (dict(preview_version=None, reason="r"), "PREVIEW_REQUIRED"),
    (dict(preview_version="deadbeef", reason="r"), "PREVIEW_STALE"),
    (dict(preview_version="x", reason=""), "INVALID_INPUT"),
])
def test_confirmed_erasure_requires_a_matching_preview_and_reason(ctx, kwargs, code):
    before = snapshot(ctx.client, "acme")
    with pytest.raises(ToolError) as e:
        forget(ctx, "user_ravi", False, authorization=RAVI, **kwargs)
    assert e.value.code == code
    assert snapshot(ctx.client, "acme") == before


def test_a_changed_dependency_after_the_preview_aborts_and_returns_a_new_preview(ctx):
    old = preview(ctx)
    remember(ctx, "user_ravi", "Ravi adopted a dog.", "episodic", "agent_concierge")
    before = snapshot(ctx.client, "acme")
    with pytest.raises(ToolError) as e:
        forget(ctx, "user_ravi", False, authorization=RAVI, preview_version=old["preview_version"], reason="r")
    assert e.value.code == "PREVIEW_STALE"
    assert e.value.details["new_preview"]["preview_version"] != old["preview_version"]
    assert snapshot(ctx.client, "acme") == before
    done = erase(ctx, version=e.value.details["new_preview"]["preview_version"])
    assert done["result"]["receipt"]["audit_result"]["graph_audit_passed"]


# ---------- fail closed ----------
def test_truncated_traversal_blocks_deletion(ctx):
    ctx.max_depth = 1
    out = forget(ctx, "user_ravi", True, authorization=RAVI)
    assert out["result"]["can_execute"] is False
    before = snapshot(ctx.client, "acme")
    with pytest.raises(ToolError) as e:
        forget(ctx, "user_ravi", False, authorization=RAVI,
               preview_version=out["result"]["preview"]["preview_version"], reason="r")
    assert e.value.code == "ERASURE_BLOCKED"
    assert snapshot(ctx.client, "acme") == before


def test_tenant_mismatch_on_a_covered_node_blocks_deletion(ctx):
    ctx.graph.query("MATCH (f:Fact {id:'fact_ravi_vegetarian'}) SET f.tenant_id = 'globex'")
    p = preview(ctx)
    assert not p["can_execute"] and p["blocking"][0]["code"] == "TENANT_MISMATCH"


def test_delete_statement_refuses_when_the_graph_changed_underneath(ctx):
    ids = preview(ctx)["node_ids"]
    rec = Recorder(ctx.graph)
    before = snapshot(ctx.client, "acme")
    ctx.graph.query("MATCH (f:Fact {id:'fact_ravi_vegetarian'}) CREATE (:Summary {id:'sum_late', tenant_id:'acme'})"
                    "-[:SUMMARIZES]->(f)")
    mid = snapshot(ctx.client, "acme")
    assert _delete_exact(rec, ids) == 0 and snapshot(ctx.client, "acme") == mid != before
    ctx.graph.query("MATCH (s:Summary {id:'sum_late'}) DETACH DELETE s")
    ctx.graph.query("MATCH (m:Memory {id:'mem_concierge_ravi'}) CREATE (m)-[:CONTAINS]->(:Fact {id:'fact_late'})")
    assert _delete_exact(rec, ids) == 0                                  # new content in a doomed Memory
    ctx.graph.query("MATCH (f:Fact {id:'fact_late'}) DETACH DELETE f")
    ids_gone = list(ids)
    ctx.graph.query("MATCH (u:User {id:'user_ravi'}) DETACH DELETE u")
    assert _delete_exact(rec, ids_gone) == 0                             # a planned node vanished
    assert snapshot(ctx.client, "acme")["nodes"].keys() >= set(ids) - {"user_ravi"}


def test_a_failing_statement_leaves_the_graph_unchanged(ctx):
    before = snapshot(ctx.client, "acme")
    with pytest.raises(ToolError) as e:
        Recorder(ctx.graph).write("MATCH (n:Action) DELETE n WITH count(*) AS c RETURN 1 / (c - c)")
    assert e.value.code == "GRAPH_ERROR"
    assert snapshot(ctx.client, "acme") == before     # observed on the graph module under test


# ---------- audit ----------
def test_audit_finds_covered_data_before_erasure_and_none_after(ctx):
    pre = audit_forgotten(ctx, "user_ravi")["result"]
    assert not pre["graph_audit_passed"] and pre["user_node_present"]
    assert set(pre["covered_nodes_remaining"]) == {d["id"] for d in find_derived(ctx, "user_ravi")["result"]["derived"]} \
        | {s["id"] for s in find_derived(ctx, "user_ravi")["result"]["user_originated"]}
    erase(ctx)
    post = audit_forgotten(ctx, "user_ravi")["result"]
    assert post["graph_audit_passed"] and post["covered_paths_remaining"] == 0 and not post["unresolved_warnings"]
    assert "does not prove" in post["limitations"]


def test_audit_reports_orphans_and_text_mentions_separately(ctx):
    erase(ctx)
    ctx.graph.query("CREATE (:Summary {id:'sum_stray', text:'Ravi enjoys cricket', tier:'semantic', "
                    "created_at:'2026-10-01T00:00:00Z', agent_id:'agent_analyst', tenant_id:'acme'})")
    out = audit_forgotten(ctx, "user_ravi", text_terms=["Ravi"])
    codes = {w["code"] for w in out["result"]["unresolved_warnings"]}
    assert {"ORPHANED_DERIVED_NODE", "POSSIBLE_TEXT_MENTION"} <= codes
    assert out["result"]["graph_audit_passed"] is True       # no covered path; the warnings are separate


def test_audit_detects_deleted_ids_that_came_back(ctx):
    plan = preview(ctx)
    erase(ctx)
    ctx.graph.query("CREATE (:Fact {id:'fact_ravi_vegetarian', text:'back', tier:'episodic', sensitivity:'low', "
                    "created_at:'2026-10-01T00:00:00Z', agent_id:'agent_concierge', tenant_id:'acme'})")
    res = audit_forgotten(ctx, "user_ravi", deleted_node_ids=plan["node_ids"])["result"]
    assert res["deleted_ids_still_present"] == ["fact_ravi_vegetarian"] and not res["graph_audit_passed"]


def test_audit_checks_other_tenants_only_with_authorization(ctx):
    plan = preview(ctx)
    erase(ctx)
    with pytest.raises(ToolError) as e:
        audit_forgotten(ctx, "user_ravi", deleted_node_ids=plan["node_ids"], tenant_scope=["acme", "globex"],
                        authorization=RAVI)
    assert e.value.code in ("FORBIDDEN_ROLE", "FORBIDDEN_TENANT")
    out = audit_forgotten(ctx, "user_ravi", deleted_node_ids=plan["node_ids"], tenant_scope=["acme", "globex"],
                          authorization=ADMIN)
    assert out["result"]["cross_tenant"]["copies_found"] == [{"tenant": "globex", "source_id": "src_xtenant_copy_lisbon"}]
    assert any(w["code"] == "CROSS_TENANT_COPY_REMAINS" for w in out["warnings"])
    assert len(out["result"]["scope"]["tenant_graphs_checked"]) == 2


# ---------- leaks ----------
def test_detect_leak_finds_the_copy_and_changes_nothing(ctx):
    before = (snapshot(ctx.client, "acme"), snapshot(ctx.client, "globex"))
    out = detect_leak(ctx, "acme", "globex", authorization=ADMIN)
    (f,) = out["result"]["findings"]
    assert (f["source_fact_id"], f["subject_user_id"], f["status"], f["declared_via_leaked_into"]) == \
           ("fact_ravi_trip_lisbon", "user_ravi", "copy_found", True)
    assert f["copies"][0]["copy_nodes"][0]["node_id"] == "fact_globex_copy_lisbon"
    rp = out["result"]["remediation_preview"]
    assert rp["executed"] is False and rp["requires_explicit_authorization"]
    assert set(rp["proposed_node_ids"]) == {"fact_globex_copy_lisbon", "src_xtenant_copy_lisbon"}
    assert (snapshot(ctx.client, "acme"), snapshot(ctx.client, "globex")) == before


def test_detect_leak_user_filter_and_reverse_direction(ctx):
    assert detect_leak(ctx, "acme", "globex", authorization=ADMIN, user_id="user_ravi")["result"]["findings"][0][
        "derives_from_requesting_user"] is True
    assert detect_leak(ctx, "acme", "globex", authorization=ADMIN, user_id="user_priya")["result"]["findings"] == []
    globex = ToolContext(ctx.client, "globex", ctx.graph_names, receipt_dir="")
    assert detect_leak(globex, "globex", "acme", authorization=ADMIN)["result"]["findings"] == []


def test_detect_leak_flags_an_undeclared_copy_and_an_edge_without_copy(ctx):
    ctx.graph.query("MATCH ()-[r:LEAKED_INTO]->() DELETE r")
    f = detect_leak(ctx, "acme", "globex", authorization=ADMIN)["result"]["findings"][0]
    assert f["status"] == "undeclared_copy" and not f["declared_via_leaked_into"]
    ctx.client.select_graph("test_tenant_globex").query("MATCH (n) DETACH DELETE n")
    ctx.graph.query("MATCH (f:Fact {id:'fact_ravi_trip_lisbon'}), (t:Tenant {id:'globex'}) CREATE (f)-[:LEAKED_INTO]->(t)")
    out = detect_leak(ctx, "acme", "globex", authorization=ADMIN)
    assert out["result"]["findings"][0]["status"] == "edge_only_no_copy_found" and out["warnings"]


@pytest.mark.parametrize("auth,code", [(None, "UNAUTHORIZED"), (RAVI, "FORBIDDEN_ROLE"),
                                       (Authorization("a", "tenant_admin", ("acme",)), "FORBIDDEN_TENANT")])
def test_detect_leak_needs_authorization_for_both_tenants(ctx, auth, code):
    with pytest.raises(ToolError) as e:
        detect_leak(ctx, "acme", "globex", authorization=auth)
    assert e.value.code == code
