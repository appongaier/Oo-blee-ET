import pytest

from app.tools import (Authorization, ToolError, act, find_derived, infer, mark_superseded, remember,
                       summarize, ToolContext)
from tests.conftest import snapshot

pytestmark = pytest.mark.integration
KEYS = {"result", "cypher", "params", "graph", "warnings"}


def edge_set(out):
    return {(e["src"], e["rel"], e["dst"]) for e in out["graph"]["edges"]}


def test_remember_creates_fact_with_full_provenance(ctx):
    out = remember(ctx, "user_priya", "Priya likes chess.", "episodic", "agent_concierge", sensitivity="medium")
    assert set(out) == KEYS and out["cypher"] and len(out["cypher"]) == len(out["params"])
    r = out["result"]
    assert {(r["fact_id"], "ABOUT", "user_priya"), (r["fact_id"], "DERIVED_FROM", r["source_id"]),
            (r["memory_id"], "CONTAINS", r["fact_id"]), ("agent_concierge", "WROTE", r["memory_id"])} <= edge_set(out)
    fact = next(n for n in out["graph"]["nodes"] if n["id"] == r["fact_id"])
    assert (fact["tier"], fact["agent_id"], fact["tenant_id"], fact["sensitivity"]) == \
           ("episodic", "agent_concierge", "acme", "medium")
    assert any("CREATE" in q for q in out["cypher"])


def test_remember_reuses_source_and_memory_in_the_same_session(ctx):
    a = remember(ctx, "user_ravi", "Ravi likes tea.", "episodic", "agent_concierge", session_id="sess_ravi_001")
    b = remember(ctx, "user_ravi", "Ravi likes jazz.", "episodic", "agent_concierge", session_id="sess_ravi_001")
    assert a["result"]["source_id"] == b["result"]["source_id"] == "src_chat_ravi_001"
    assert a["result"]["memory_id"] == b["result"]["memory_id"] == "mem_concierge_ravi"


@pytest.mark.parametrize("kwargs,code", [
    (dict(user_id="user_nobody"), "USER_NOT_FOUND"),
    (dict(agent_id="agent_nobody"), "AGENT_NOT_FOUND"),
    (dict(agent_id="agent_auditor"), "ROLE_NOT_ALLOWED"),
    (dict(tier="long_term"), "INVALID_INPUT"),
    (dict(text="  "), "INVALID_INPUT"),
    (dict(text="x" * 1001), "INVALID_INPUT"),
    (dict(user_id="user_ravi'; DROP"), "INVALID_INPUT"),
    (dict(session_id="Bad Session"), "INVALID_INPUT"),
])
def test_remember_rejects_bad_input_and_changes_nothing(ctx, kwargs, code):
    args = dict(user_id="user_ravi", text="ok text", tier="episodic", agent_id="agent_concierge")
    args.update(kwargs)
    before = snapshot(ctx.client, "acme")
    with pytest.raises(ToolError) as e:
        remember(ctx, **args)
    assert e.value.code == code
    assert snapshot(ctx.client, "acme") == before


def test_remember_cannot_reach_another_tenants_user(ctx):
    globex = ToolContext(ctx.client, "globex", ctx.graph_names, receipt_dir="")
    with pytest.raises(ToolError) as e:
        remember(globex, "user_ravi", "leak attempt", "episodic", "agent_analyst")
    assert e.value.code == "USER_NOT_FOUND"


def test_summarize_infer_act_build_full_lineage(ctx):
    f = remember(ctx, "user_priya", "Priya runs on Sundays.", "episodic", "agent_concierge")["result"]["fact_id"]
    s = summarize(ctx, "agent_analyst", [f, "sum_priya_profile"], "Priya runs and works in finance.")
    sid = s["result"]["summary_id"]
    assert {(sid, "SUMMARIZES", f), (sid, "SUMMARIZES", "sum_priya_profile")} <= edge_set(s)
    i = infer(ctx, "agent_analyst", [f], "Priya is active.", confidence=0.7)
    iid = i["result"]["inference_id"]
    node = next(n for n in i["graph"]["nodes"] if n["id"] == iid)
    assert (node["confidence"], node["tier"], node["agent_id"], node["tenant_id"]) == (0.7, "semantic", "agent_analyst", "acme")
    a = act(ctx, "agent_analyst", iid, "send_fitness_tip")
    aid = a["result"]["action_id"]
    assert (aid, "BASED_ON", iid) in edge_set(a)
    act_node = next(n for n in a["graph"]["nodes"] if n["id"] == aid)
    assert act_node["status"] == "proposed" and any(w["code"] == "NOT_EXECUTED" for w in a["warnings"])
    derived = {d["id"] for d in find_derived(ctx, "user_priya")["result"]["derived"]}
    assert {sid, iid, aid} <= derived


@pytest.mark.parametrize("call", [
    lambda c: summarize(c, "agent_analyst", ["fact_ravi_vegetarian", "missing_id"], "x"),
    lambda c: summarize(c, "agent_analyst", ["act_ravi_suggest_restaurants"], "x"),
    lambda c: act(c, "agent_analyst", "fact_ravi_vegetarian", "send_note"),
    lambda c: infer(c, "agent_analyst", ["fact_ravi_vegetarian"], "x", confidence=1.5),
    lambda c: summarize(c, "agent_analyst", [], "x"),
    lambda c: summarize(c, "agent_analyst", ["fact_ravi_vegetarian", "fact_ravi_vegetarian"], "x"),
    lambda c: summarize(c, "agent_concierge", ["fact_ravi_vegetarian"], "x"),
    lambda c: summarize(c, "agent_auditor", ["fact_ravi_vegetarian"], "x"),
    lambda c: act(c, "agent_analyst", "inf_ravi_traveler", "Bad Type!"),
])
def test_derive_tools_reject_bad_parents_and_change_nothing(ctx, call):
    before = snapshot(ctx.client, "acme")
    with pytest.raises(ToolError):
        call(ctx)
    assert snapshot(ctx.client, "acme") == before


def test_parent_from_another_tenant_is_rejected(ctx):
    globex = ToolContext(ctx.client, "globex", ctx.graph_names, receipt_dir="")
    with pytest.raises(ToolError) as e:
        summarize(globex, "agent_analyst", ["fact_ravi_vegetarian"], "x")
    assert e.value.code == "PARENT_NOT_FOUND"


def test_summarizing_a_superseded_fact_warns(ctx):
    out = summarize(ctx, "agent_analyst", ["fact_ravi_city_pune"], "Ravi once lived in Pune.")
    assert [w["code"] for w in out["warnings"]] == ["SUPERSEDED_PARENT"]


def test_mark_superseded_keeps_history_and_stays_covered(ctx):
    new = remember(ctx, "user_priya", "Priya moved to the product team.", "episodic", "agent_concierge")["result"]["fact_id"]
    out = mark_superseded(ctx, "fact_priya_role_finance", new)
    assert out["result"]["old_fact_deleted"] is False
    assert ("fact_priya_role_finance", "SUPERSEDED_BY", new) in edge_set(out)
    assert any(w["code"] == "COVERED_BY_ERASURE" for w in out["warnings"])
    seeds = {s["id"]: s["superseded"] for s in find_derived(ctx, "user_priya")["result"]["user_originated"]}
    assert seeds["fact_priya_role_finance"] is True and new in seeds


@pytest.mark.parametrize("old,new,code", [
    ("fact_ravi_city_pune", "fact_ravi_city_pune", "INVALID_INPUT"),
    ("fact_ravi_vegetarian", "fact_priya_email_pref", "SUBJECT_MISMATCH"),
    ("fact_ravi_city_pune", "fact_ravi_vegetarian", "ALREADY_SUPERSEDED"),
    ("fact_ravi_city_bengaluru", "fact_ravi_city_pune", "CYCLE"),
    ("fact_ravi_vegetarian", "fact_nobody", "FACT_NOT_FOUND"),
    ("sum_ravi_profile", "fact_ravi_vegetarian", "FACT_NOT_FOUND"),
])
def test_mark_superseded_rejections(ctx, old, new, code):
    before = snapshot(ctx.client, "acme")
    with pytest.raises(ToolError) as e:
        mark_superseded(ctx, old, new)
    assert e.value.code == code
    assert snapshot(ctx.client, "acme") == before
