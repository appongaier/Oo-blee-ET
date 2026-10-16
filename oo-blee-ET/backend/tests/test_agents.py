"""Agent behaviour with a scripted LLM against a live FalkorDB. The scripted LLM returns real
Anthropic-shaped blocks, so the loop, gateway, grounding checks and handoffs run for real; only the
model's choices are scripted."""
import itertools
import json
import re

import pytest

from app.agents import LLMError, LLMResponse, Orchestrator, run_agent
from app.agents.gateway import TOOLS
from app.mcp_support import McpToolError
from app.agents.runtime import SYSTEM_INSTRUCTION
from app.tools import Authorization
from tests.conftest import TEST_NAMES, snapshot

pytestmark = pytest.mark.integration
KEYS = {"agent_id", "response", "path", "cypher", "tool_trace", "warnings"}
RAVI = Authorization("user_ravi", "data_subject", ("acme",))
PRIYA = Authorization("user_priya", "data_subject", ("acme",))
_ids = itertools.count(1)


def say(text):
    return {"type": "text", "text": text}


def use(name, **inp):
    return {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": inp}


def results(messages):
    out = []
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            out += [json.loads(b["content"]) for b in m["content"] if b["type"] == "tool_result"]
    return out


def found(messages, key):
    return [r["result"][key] for r in results(messages) if "result" in r and key in r["result"]]


class ScriptedLLM:
    def __init__(self, *turns):
        self.turns, self.requests = list(turns), []

    def create(self, *, system, messages, tools):
        self.requests.append({"system": system, "tools": tools, "messages": json.loads(json.dumps(messages))})
        turn = self.turns.pop(0)
        blocks = turn(messages) if callable(turn) else turn
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return LLMResponse(blocks=blocks, stop_reason=stop)


_ORCHS = []


@pytest.fixture(autouse=True)
def _close_mcp_servers():
    yield
    while _ORCHS:
        _ORCHS.pop().close()


def orch(ctx, llm, user="user_ravi"):
    """An orchestrator whose agents reach the graph over real MCP servers (separate processes)."""
    o = Orchestrator("acme", user, llm, graph_names=TEST_NAMES, receipt_dir=ctx.receipt_dir,
                     log_path=f"{ctx.receipt_dir}/mcp.jsonl")
    _ORCHS.append(o)
    return o


# ---------- single agents ----------
def test_concierge_records_a_fact_and_returns_graph_evidence(ctx):
    llm = ScriptedLLM([use("remember", text="Ravi plays chess on weekends.")],
                      lambda m: [say(f"Recorded [{found(m, 'fact_id')[0]}].")])
    out = orch(ctx, llm).run("concierge", "Ravi says he plays chess on weekends.")
    assert set(out) == KEYS and out["agent_id"] == "agent_concierge" and out["warnings"] == []
    (t,) = out["tool_trace"]
    assert (t["tool"], t["status"], t["agent_id"]) == ("remember", "ok", "agent_concierge")
    assert t["args"]["text"] == f"<{len('Ravi plays chess on weekends.')} chars>" and t["statements"] > 0 and t["seq"] == 1
    rels = {e["rel"] for p in out["path"] for e in p["edges"]}
    assert {"ABOUT", "DERIVED_FROM", "CONTAINS"} <= rels
    assert out["cypher"] and all(isinstance(q, str) for q in out["cypher"])
    fid = re.search(r"\[(fact_\w+)\]", out["response"]).group(1)
    g = ctx.graph.ro_query("MATCH (f:Fact {id:$i})-[:ABOUT]->(u:User) RETURN u.id, f.agent_id, f.tier", {"i": fid})
    assert g.result_set == [["user_ravi", "agent_concierge", "episodic"]]


def test_trace_never_contains_personal_text(ctx):
    llm = ScriptedLLM([use("remember", text="Ravi collects vintage maps.")], [say("ok")])
    out = orch(ctx, llm).run("concierge", "hi")
    assert "vintage maps" not in json.dumps(out["tool_trace"])


def test_prompts_and_tool_lists(ctx):
    llm = ScriptedLLM([say("a")], [say("b")], [say("c")])
    o = orch(ctx, llm)
    for role in ("concierge", "analyst", "auditor"):
        o.run(role, "hello")
    assert all(r["system"].startswith(SYSTEM_INSTRUCTION) for r in llm.requests)
    names = [{t["name"] for t in r["tools"]} for r in llm.requests]
    assert names[0] == {"remember", "mark_superseded", "find_derived", "explain_belief"}
    assert names[1] == {"find_derived", "explain_belief", "trace_provenance", "summarize", "infer", "act"}
    assert names[2] == {"find_derived", "explain_belief", "trace_provenance", "audit_forgotten", "detect_leak",
                        "forget_preview"}
    forbidden = {"agent_id", "tenant_id", "user_id", "dry_run", "preview_version", "reason", "authorization", "tier"}
    for spec in TOOLS.values():
        assert not forbidden & set(spec.properties)
    assert "forget" not in TOOLS and not any("erase" in n for n in TOOLS)


@pytest.mark.parametrize("role,calls", [
    ("concierge", [("summarize", dict(fact_ids=["fact_ravi_vegetarian"], text="x"))]),
    ("concierge", [("remember", dict(text="x", agent_id="agent_analyst"))]),
    ("concierge", [("remember", dict(text="x", user_id="user_priya"))]),
    ("concierge", [("remember", dict(text="x", tier="semantic"))]),
    ("analyst", [("remember", dict(text="x"))]),
    ("auditor", [("remember", dict(text="x"))]),
    ("auditor", [("forget_preview", dict(dry_run=False))]),
    ("auditor", [("forget", dict(user_id="user_ravi", dry_run=False))]),
    ("auditor", [("forget_preview", dict(preview_version="x", reason="x"))]),
    ("auditor", [("summarize", dict(fact_ids=["fact_ravi_vegetarian"], text="x"))]),
    ("analyst", [("summarize", dict(fact_ids="fact_ravi_vegetarian", text="x"))]),
    ("analyst", [("infer", dict(fact_ids=["fact_ravi_vegetarian"], text="x", confidence=7))]),
])
def test_gateway_blocks_what_an_agent_may_not_do(ctx, role, calls):
    before = snapshot(ctx.client, "acme")
    llm = ScriptedLLM([use(n, **a) for n, a in calls], [say("done")])
    out = orch(ctx, llm).run(role, "go")
    assert snapshot(ctx.client, "acme") == before
    assert [t["status"] for t in out["tool_trace"]] == ["rejected"] * len(calls)
    assert [w["code"] for w in out["warnings"] if w["code"] == "TOOL_ERROR"] == ["TOOL_ERROR"] * len(calls)
    assert any(r.get("error") for r in results(llm.requests[1]["messages"]))     # the model was told


def test_subject_is_injected_so_priya_sessions_write_about_priya(ctx):
    llm = ScriptedLLM([use("remember", text="Priya likes pottery.")], lambda m: [say(f"[{found(m, 'fact_id')[0]}]")])
    out = orch(ctx, llm, "user_priya").run("concierge", "Priya likes pottery.")
    fid = re.search(r"\[(fact_\w+)\]", out["response"]).group(1)
    rows = ctx.graph.ro_query("MATCH (f:Fact {id:$i})-[:ABOUT]->(u) RETURN u.id", {"i": fid}).result_set
    assert rows == [["user_priya"]]


def test_analyst_derivation_flow_writes_lineage_and_returns_paths(ctx):
    facts = ["fact_ravi_vegetarian", "fact_ravi_trip_lisbon"]
    llm = ScriptedLLM(
        [use("find_derived")],
        [use("summarize", fact_ids=facts, text="Ravi eats vegetarian and plans a Lisbon trip.")],
        lambda m: [use("infer", fact_ids=facts + found(m, "summary_id"), text="Ravi wants vegetarian food in Lisbon.",
                       confidence=0.8)],
        lambda m: [use("act", inference_ids=found(m, "inference_id"), action_type="send_lisbon_veg_guide")],
        lambda m: [say(f"Created [{found(m, 'summary_id')[0]}], [{found(m, 'inference_id')[0]}], "
                       f"[{found(m, 'action_id')[0]}] from [fact_ravi_vegetarian].")])
    arts = {}
    out = run_agent("analyst", orch(ctx, llm).gateway, llm, "Analyse Ravi's facts.", artifacts=arts)
    assert out["warnings"] == [] and [t["status"] for t in out["tool_trace"]] == ["ok"] * 4
    assert {"summary_id", "inference_id", "action_id"} <= set(arts)
    rels = {e["rel"] for p in out["path"] for e in p["edges"]}
    assert {"SUMMARIZES", "INFERRED_FROM", "BASED_ON"} <= rels
    n = ctx.graph.ro_query("MATCH (n) WHERE n.id IN $ids RETURN DISTINCT n.agent_id",
                           {"ids": [i for v in arts.values() for i in v]}).result_set
    assert n == [["agent_analyst"]]


def test_reads_return_text_and_lineage_for_the_model(ctx):
    llm = ScriptedLLM([use("find_derived")], [say("ok")])
    orch(ctx, llm).run("concierge", "What do you know about Ravi?")
    res = results(llm.requests[1]["messages"])[0]
    texts = [n.get("text") for n in res["nodes"]]
    assert "Ravi eats vegetarian food." in texts and "cypher" not in json.dumps(res)


# ---------- grounding ----------
def test_answer_about_a_user_without_graph_evidence_is_flagged(ctx):
    llm = ScriptedLLM([say("Ravi is vegetarian [fact_made_up].")])
    out = orch(ctx, llm).run("analyst", "What do you know about Ravi?")
    assert out["response"].startswith("Evidence unavailable")
    assert {w["code"] for w in out["warnings"]} == {"NO_GRAPH_EVIDENCE", "UNSUPPORTED_CITATION"}


def test_invented_ids_are_flagged_even_when_other_evidence_exists(ctx):
    llm = ScriptedLLM([use("find_derived")], [say("Ravi is vegetarian [fact_ravi_vegetarian] and a pilot [fact_pilot].")])
    out = orch(ctx, llm).run("concierge", "Tell me about Ravi")
    assert [w["code"] for w in out["warnings"]] == ["UNSUPPORTED_CITATION"]
    assert "fact_pilot" in out["response"].split("(Note:")[1] and "fact_ravi_vegetarian" not in out["response"].split("(Note:")[1]


def test_a_grounded_answer_has_no_warnings(ctx):
    llm = ScriptedLLM([use("explain_belief", node_id="sum_team_q4")], [say("It summarizes [fact_ravi_owns_onboarding].")])
    out = orch(ctx, llm).run("auditor", "Why does sum_team_q4 exist?")
    assert out["warnings"] == [] and any(p["source_tool"] == "explain_belief" for p in out["path"])


def test_ids_supplied_by_the_caller_are_not_treated_as_invented(ctx):
    llm = ScriptedLLM([say("I was handed [fact_ravi_vegetarian] but have not read it.")])
    out = run_agent("analyst", orch(ctx, llm).gateway, llm, "Look at fact_ravi_vegetarian")
    assert "UNSUPPORTED_CITATION" not in {w["code"] for w in out["warnings"]}


# ---------- failures ----------
def test_tool_errors_reach_the_model_and_the_caller(ctx):
    before = snapshot(ctx.client, "acme")
    llm = ScriptedLLM([use("summarize", fact_ids=["fact_does_not_exist"], text="x")], [say("That failed.")])
    out = orch(ctx, llm).run("analyst", "summarize it")
    assert out["tool_trace"][0]["status"] == "error" and out["tool_trace"][0]["error_code"] == "PARENT_NOT_FOUND"
    assert results(llm.requests[1]["messages"])[0]["error"]["code"] == "PARENT_NOT_FOUND"
    assert llm.requests[1]["messages"][-1]["content"][0]["is_error"] is True
    assert [w["code"] for w in out["warnings"]] == ["TOOL_ERROR"] and snapshot(ctx.client, "acme") == before


def test_runaway_tool_loops_are_stopped(ctx):
    llm = ScriptedLLM(*[[use("find_derived")] for _ in range(10)])
    out = run_agent("analyst", orch(ctx, llm).gateway, llm, "go", max_steps=3)
    assert len(out["tool_trace"]) == 3 and "MAX_STEPS_REACHED" in {w["code"] for w in out["warnings"]}


def test_an_unreachable_model_yields_an_honest_non_answer(ctx):
    class Down:
        def create(self, **kw):
            raise LLMError("APIConnectionError: down")
    out = orch(ctx, Down()).run("concierge", "hi")
    assert set(out) == KEYS and out["tool_trace"] == [] and out["path"] == []
    assert [w["code"] for w in out["warnings"]] == ["LLM_ERROR"] and "unavailable" in out["response"]


# ---------- handoffs ----------
def _pipeline_llms():
    concierge = ScriptedLLM(
        [use("remember", text="Ravi plays chess on weekends."), use("remember", text="Ravi is learning Portuguese.")],
        lambda m: [say("Recorded " + ", ".join(f"[{i}]" for i in found(m, "fact_id")) + ".")])
    facts = lambda m: re.findall(r"fact_\w+", m[0]["content"])  # noqa: E731
    analyst = ScriptedLLM(
        [use("find_derived")],
        lambda m: [use("summarize", fact_ids=facts(m), text="Ravi plays chess and is learning Portuguese.")],
        lambda m: [use("infer", fact_ids=facts(m), text="Ravi enjoys structured learning.", confidence=0.55)],
        lambda m: [use("act", inference_ids=found(m, "inference_id"), action_type="suggest_study_group")],
        lambda m: [say(f"Created [{found(m, 'summary_id')[0]}] and [{found(m, 'action_id')[0]}].")])
    derived = lambda m: re.findall(r"(?:sum|inf|act)_\w+", m[0]["content"])  # noqa: E731
    auditor = ScriptedLLM(
        lambda m: [use("explain_belief", node_id=derived(m)[0]), use("explain_belief", node_id=derived(m)[-1])],
        lambda m: [say("Both are supported: " + ", ".join(f"[{d}]" for d in derived(m)[:1]) + ".")])
    return {"concierge": concierge, "analyst": analyst, "auditor": auditor}


def test_pipeline_hands_off_real_ids_in_order(ctx):
    o = orch(ctx, _pipeline_llms())
    out = o.run_pipeline(["Ravi plays chess on weekends.", "Ravi is learning Portuguese."])
    assert "stopped" not in out
    h = out["handoffs"]
    assert [(x["seq"], x["from"], x["to"]) for x in h] == [(1, "agent_concierge", "agent_analyst"),
                                                           (2, "agent_analyst", "agent_auditor")]
    assert h[0]["node_ids"] == out["created"]["fact_id"] and len(h[0]["node_ids"]) == 2
    assert set(h[1]["node_ids"]) == {i for k in ("summary_id", "inference_id", "action_id") for i in out["created"][k]}
    for role in ("concierge", "analyst", "auditor"):
        assert set(out[role]) == KEYS and out[role]["warnings"] == []
    seqs = [t["seq"] for r in ("concierge", "analyst", "auditor") for t in out[r]["tool_trace"]]
    assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1))
    assert out["auditor"]["path"] and all(t["status"] == "ok" for t in out["auditor"]["tool_trace"])


def test_pipeline_stops_when_the_concierge_records_nothing(ctx):
    out = orch(ctx, {"concierge": ScriptedLLM([say("Sure, noted!")]), "analyst": ScriptedLLM(),
                     "auditor": ScriptedLLM()}).run_pipeline(["Ravi likes tea."])
    assert out["handoffs"] == [] and "nothing to hand off" in out["stopped"]
    assert out["concierge"]["tool_trace"] == []


# ---------- erasure workflow ----------
def test_llms_cannot_erase_but_the_workflow_can_with_authorization(ctx):
    preview_llm = ScriptedLLM([use("forget_preview")], lambda m: [say(
        f"Would delete {len(results(m)[0]['result']['preview']['node_ids'])} nodes incl. [user_ravi].")])
    o = orch(ctx, {"concierge": ScriptedLLM(), "analyst": ScriptedLLM(), "auditor": preview_llm})
    before = snapshot(ctx.client, "acme")
    pv = o.preview_erasure()
    assert snapshot(ctx.client, "acme") == before and pv["can_execute"]
    assert pv["auditor"]["warnings"] == [] and pv["auditor"]["tool_trace"][0]["tool"] == "forget_preview"
    assert len(pv["preview"]["node_ids"]) == 17

    version = pv["preview"]["preview_version"]
    for bad in (PRIYA, Authorization("agent_auditor", "auditor", ("acme",))):
        with pytest.raises(McpToolError):
            o.execute_erasure(version, "request", bad)
    assert snapshot(ctx.client, "acme") == before

    post_llm = ScriptedLLM([use("audit_forgotten"), use("detect_leak", other_tenant="globex")],
                           lambda m: [say("Graph audit passed for [user_ravi]'s data; copies in globex remain.")])
    o.llms["auditor"] = post_llm
    done = o.execute_erasure(version, "user request", RAVI)
    receipt = done["receipt"]
    assert receipt["audit_result"]["graph_audit_passed"] and len(receipt["deleted_node_ids"]) == 17
    audit_res, leak_res = results(post_llm.requests[1]["messages"])
    assert audit_res["result"]["graph_audit_passed"] is True
    assert [f["status"] for f in leak_res["result"]["findings"]] == ["undeclared_copy"]
    assert o.session.deleted_node_ids == receipt["deleted_node_ids"]
    assert done["auditor"]["tool_trace"][0]["tool"] == "audit_forgotten"
    assert [h["from"] for h in done["handoffs"]] == ["orchestrator", "orchestrator"]
    left = ctx.graph.ro_query("MATCH (u:User) RETURN u.id").result_set
    assert left == [["user_priya"]]


def test_session_rejects_unknown_subjects(ctx):
    with pytest.raises(McpToolError) as e:
        orch(ctx, ScriptedLLM(), "user_nobody")
    assert e.value.code in ("FORBIDDEN_SUBJECT", "USER_NOT_FOUND")
