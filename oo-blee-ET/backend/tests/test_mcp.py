"""The MCP layer, end to end: real server processes (mcp/server.py, official SDK, stdio) and the real client.
Nothing here calls the tool functions through Python except where a test compares against them on purpose."""
import ast
import json
import os
from pathlib import Path

import pytest

from app import agents as agents_pkg
from app.agents import McpToolClient, Orchestrator, ServerBinding
from app.mcp_support import ALL_TOOLS, ROLE_TOOLS, AuthorizationClaim, McpToolError, read_log, reconstruct_handoffs
from app.tools import Authorization, ToolContext, find_derived
from app.tools import __all__ as tool_exports
from tests.conftest import TEST_NAMES, expected_erasure, snapshot
from tests.test_agents import ScriptedLLM, _pipeline_llms, say, use

pytestmark = pytest.mark.integration
RAVI = AuthorizationClaim("user_ravi", "data_subject", ("acme",))


@pytest.fixture
def mcp(ctx):
    """Factory for MCP clients bound to the throwaway test graphs. Closed at teardown."""
    made = []
    log = f"{ctx.receipt_dir}/mcp.jsonl"

    def make(role, subject="user_ravi", auditor_tenants=("acme", "globex")):
        c = McpToolClient(role, ServerBinding("acme", subject, "run_mcp_test", auditor_tenants, TEST_NAMES,
                                              ctx.receipt_dir, log)).start()
        made.append(c)
        return c

    make.log = log
    yield make
    for c in made:
        c.close()


def calls(log, **match):
    return [r for r in read_log(log) if r["event"] == "tool_call" and all(r.get(k) == v for k, v in match.items())]


# ---------- what each server exposes ----------
def test_each_role_exposes_exactly_its_tools_and_together_they_cover_all_eleven(mcp):
    exposed = {}
    for role, tools in ROLE_TOOLS.items():
        exposed[role] = mcp(role).tools
        assert exposed[role] == set(tools)
    assert set().union(*exposed.values()) == set(ALL_TOOLS) and len(ALL_TOOLS) == 11
    assert set(ALL_TOOLS) == set(tool_exports) - {"Authorization", "ToolContext", "ToolError"}


def test_published_schemas_differ_by_role(mcp):
    op, aud, con = mcp("operator"), mcp("auditor"), mcp("concierge")
    assert {"authorization", "preview_version", "reason", "dry_run"} <= set(op.tool_schemas["forget"]["properties"])
    assert set(aud.tool_schemas["forget"]["properties"]) == {"user_id", "dry_run"}
    props = con.tool_schemas["remember"]["properties"]
    assert props["tier"]["enum"] == ["episodic", "semantic", "procedural"] and "agent_id" not in props
    assert "tenant_id" not in json.dumps(con.tool_schemas)


def test_results_are_structured_and_match_the_tool_itself(ctx, mcp):
    res = mcp("concierge").call("find_derived", {"user_id": "user_ravi"})
    assert res["ok"] and set(res["output"]) == {"result", "cypher", "params", "graph", "warnings"}
    direct = find_derived(ToolContext(ctx.client, "acme", TEST_NAMES), "user_ravi")
    for key in ("user_originated", "derived", "node_ids", "complete"):
        assert res["output"]["result"][key] == direct["result"][key]
    assert res["output"]["result"]["user"]["display_name"] == "Ravi"
    assert res["output"]["cypher"] and len(res["output"]["cypher"]) == len(res["output"]["params"])


# ---------- schema validation ----------
@pytest.mark.parametrize("args", [
    dict(user_id="user_ravi", text="x", tier="long_term"),
    dict(user_id="user_ravi", tier="episodic"),
    dict(user_id="user_ravi", text="x", tier="episodic", agent_id="agent_analyst"),
    dict(user_id="user_ravi", text="x" * 1001, tier="episodic"),
    dict(user_id="user_ravi; DROP", text="x", tier="episodic"),
    dict(user_id=5, text="x", tier="episodic"),
    dict(user_id="user_ravi", text="x", tier="episodic", session_id="Bad Session"),
    dict(user_id="user_ravi", text="x", tier="episodic", sensitivity="extreme"),
])
def test_bad_input_is_rejected_by_the_server_before_any_tool_runs(ctx, mcp, args):
    c = mcp("concierge")
    before = snapshot(ctx.client, "acme")
    res = c.call("remember", args)
    assert not res["ok"] and res["error"]["code"] == "INVALID_ARGUMENTS"
    assert snapshot(ctx.client, "acme") == before
    assert calls(mcp.log, tool="remember")[-1]["status"] == "rejected"


# ---------- tenant, subject and role binding ----------
def test_tenant_and_subject_are_bound_at_launch(ctx, mcp):
    con, aud = mcp("concierge"), mcp("auditor", auditor_tenants=("acme",))
    for tool, args in (("remember", dict(user_id="user_priya", text="x", tier="episodic")),
                       ("find_derived", dict(user_id="user_priya"))):
        assert con.call(tool, args)["error"]["code"] == "FORBIDDEN_SUBJECT"
    assert aud.call("detect_leak", dict(tenant_a="acme", tenant_b="globex"))["error"]["code"] == "FORBIDDEN_TENANT"
    assert mcp("auditor").call("detect_leak", dict(tenant_a="globex", tenant_b="acme"))["error"]["code"] == "TENANT_MISMATCH"
    assert mcp("auditor").call("detect_leak", dict(tenant_a="acme", tenant_b="globex"))["ok"]


@pytest.mark.parametrize("role,tool,args", [
    ("auditor", "remember", dict(user_id="user_ravi", text="x", tier="episodic")),
    ("analyst", "remember", dict(user_id="user_ravi", text="x", tier="episodic")),
    ("concierge", "summarize", dict(fact_ids=["fact_ravi_vegetarian"], text="x")),
    ("auditor", "summarize", dict(fact_ids=["fact_ravi_vegetarian"], text="x")),
    ("analyst", "forget", dict(user_id="user_ravi")),
    ("concierge", "forget", dict(user_id="user_ravi")),
    ("concierge", "audit_forgotten", dict(user_id="user_ravi")),
])
def test_a_server_refuses_tools_outside_its_role_even_without_the_gateway(ctx, mcp, role, tool, args):
    before = snapshot(ctx.client, "acme")
    res = mcp(role).call(tool, args)
    assert res["error"]["code"] == "TOOL_NOT_EXPOSED" and snapshot(ctx.client, "acme") == before
    assert calls(mcp.log, tool=tool, role=role)[-1]["status"] == "rejected"


def test_concierge_policy_allows_episodic_memories_only(ctx, mcp):
    res = mcp("concierge").call("remember", dict(user_id="user_ravi", text="x", tier="semantic"))
    assert res["error"]["code"] == "FORBIDDEN_POLICY"


# ---------- erasure over MCP ----------
def test_the_auditor_can_preview_but_never_erase(ctx, mcp):
    aud, before = mcp("auditor"), snapshot(ctx.client, "acme")
    assert aud.call("forget", dict(user_id="user_ravi", dry_run=False))["error"]["code"] == "FORBIDDEN_DESTRUCTIVE"
    for extra in (dict(preview_version="a" * 64), dict(reason="x"), dict(authorization=RAVI.__dict__)):
        assert aud.call("forget", dict(user_id="user_ravi", dry_run=False, **extra))["error"]["code"] == "INVALID_ARGUMENTS"
    ok = aud.call("forget", dict(user_id="user_ravi"))
    assert ok["ok"] and ok["output"]["result"]["mutated"] is False and len(ok["output"]["result"]["preview"]["node_ids"]) == 17
    assert snapshot(ctx.client, "acme") == before


def test_the_operator_needs_a_valid_authorization_and_preview_to_erase(ctx, mcp):
    op, before = mcp("operator"), snapshot(ctx.client, "acme")
    pv = op.call("forget", dict(user_id="user_ravi", authorization={"principal_id": "user_ravi", "role": "data_subject",
                                                                     "tenant_ids": ["acme"]}))
    version = pv["output"]["result"]["preview"]["preview_version"]
    base = dict(user_id="user_ravi", dry_run=False, preview_version=version, reason="request")
    cases = [(None, "UNAUTHORIZED"),
             ({"principal_id": "agent_auditor", "role": "auditor", "tenant_ids": ["acme"]}, "FORBIDDEN_ROLE"),
             ({"principal_id": "user_priya", "role": "data_subject", "tenant_ids": ["acme"]}, "FORBIDDEN_SUBJECT"),
             ({"principal_id": "user_ravi", "role": "data_subject", "tenant_ids": ["globex"]}, "FORBIDDEN_TENANT"),
             ({"principal_id": "user_ravi", "role": "superuser", "tenant_ids": ["acme"]}, "INVALID_ARGUMENTS")]
    for auth, code in cases:
        res = op.call("forget", {**base, **({"authorization": auth} if auth else {})})
        assert res["error"]["code"] == code, (auth, res)
    assert snapshot(ctx.client, "acme") == before
    stale = op.call("forget", {**base, "preview_version": "0" * 64, "authorization": RAVI.__dict__ | {"tenant_ids": ["acme"]}})
    assert stale["error"]["code"] == "PREVIEW_STALE" and stale["error"]["details"]["new_preview"]["preview_version"] == version


def test_a_confirmed_erasure_over_mcp_deletes_exactly_the_plan(ctx, mcp):
    op = mcp("operator")
    claim = {"principal_id": "user_ravi", "role": "data_subject", "tenant_ids": ["acme"]}
    version = op.call("forget", dict(user_id="user_ravi", authorization=claim))["output"]["result"]["preview"]["preview_version"]
    done = op.call("forget", dict(user_id="user_ravi", dry_run=False, preview_version=version, reason="request",
                                  authorization=claim))
    receipt = done["output"]["result"]["receipt"]
    assert set(receipt["deleted_node_ids"]) == expected_erasure("user_ravi")
    assert not set(receipt["deleted_node_ids"]) & set(snapshot(ctx.client, "acme")["nodes"])
    assert receipt["audit_result"]["graph_audit_passed"]


def test_tool_errors_are_structured_and_the_server_keeps_working(ctx, mcp):
    an = mcp("analyst")
    res = an.call("summarize", dict(fact_ids=["fact_nope"], text="x"))
    assert res["error"]["code"] == "PARENT_NOT_FOUND" and "fact_nope" in res["error"]["message"]
    assert an.call("find_derived", dict(user_id="user_ravi"))["ok"]


def test_a_closed_server_reports_a_transport_error_instead_of_hanging(mcp):
    c = mcp("analyst")
    c.close()
    assert c.call("find_derived", dict(user_id="user_ravi"))["error"]["code"] == "MCP_TRANSPORT_ERROR"


# ---------- logging ----------
def test_the_audit_log_has_ids_status_and_reasons_but_no_personal_text(ctx, mcp):
    c = mcp("concierge")
    c.call("remember", dict(user_id="user_ravi", text="Ravi collects vintage maps.", tier="episodic"),
           {"trace_id": "t-1", "seq": 1, "reason": "recording a stated fact"})
    row = calls(mcp.log, tool="remember")[0]
    assert (row["status"], row["trace_id"], row["reason"], row["role"], row["agent_id"], row["tenant"]) == \
           ("ok", "t-1", "recording a stated fact", "concierge", "agent_concierge", "acme")
    assert row["arg_keys"] == ["text", "tier", "user_id"] and row["statements"] > 0 and row["pid"] != os.getpid()
    assert "vintage maps" not in Path(mcp.log).read_text()
    start = [r for r in read_log(mcp.log) if r["event"] == "server_start"][0]
    assert start["tools"] == list(ROLE_TOOLS["concierge"])


def _run_pipeline(ctx):
    o = Orchestrator("acme", "user_ravi", _pipeline_llms(), graph_names=TEST_NAMES, receipt_dir=ctx.receipt_dir,
                     log_path=f"{ctx.receipt_dir}/mcp.jsonl", run_id="run_pipe_1")
    try:
        return o, o.run_pipeline(["Ravi plays chess on weekends.", "Ravi is learning Portuguese."])
    finally:
        o.close()


def test_the_handoff_sequence_can_be_rebuilt_from_the_log_alone(ctx):
    o, out = _run_pipeline(ctx)
    log = f"{ctx.receipt_dir}/mcp.jsonl"
    rebuilt = reconstruct_handoffs(log, "run_pipe_1")
    assert [(h["seq"], h["from"], h["to"], h["node_ids"]) for h in rebuilt] == \
           [(h["seq"], h["from"], h["to"], h["node_ids"]) for h in out["handoffs"]] and len(rebuilt) == 2
    rows = read_log(log, "run_pipe_1")
    starts = [r["agent_id"] for r in rows if r["event"] == "agent_turn_start"]
    assert starts == ["agent_concierge", "agent_analyst", "agent_auditor"]
    # each model-chosen tool call appears as a selection (orchestrator) and an execution (server) with one trace id
    selected = {r["trace_id"]: r for r in rows if r["event"] == "tool_selected"}
    executed = {r["trace_id"]: r for r in rows if r["event"] == "tool_call" and r["trace_id"] in selected}
    assert selected and set(selected) == set(executed)
    assert all(executed[t]["agent_id"] == selected[t]["agent_id"] and executed[t]["status"] == "ok" for t in selected)
    order = [r["event"] for r in rows if r["event"] in ("agent_turn_start", "handoff")]
    assert order == ["agent_turn_start", "handoff", "agent_turn_start", "handoff", "agent_turn_start"]


def test_stated_reasons_are_logged_with_the_subjects_name_masked(ctx):
    llm = ScriptedLLM([say("Recording that Ravi plays chess, as RAVI said."), use("remember", text="Ravi plays chess.")],
                      [say("done")])
    log = f"{ctx.receipt_dir}/mcp.jsonl"
    with Orchestrator("acme", "user_ravi", llm, graph_names=TEST_NAMES, receipt_dir=ctx.receipt_dir, log_path=log,
                      run_id="run_reason") as o:
        o.run("concierge", "go")
    text = Path(log).read_text()
    rows = [r for r in read_log(log, "run_reason") if r["event"] in ("tool_selected", "tool_call") and r.get("tool") == "remember"]
    assert len(rows) == 2 and all(r["reason"] == "Recording that <subject> plays chess, as <subject> said." for r in rows)
    assert "Ravi plays chess" not in text


# ---------- no bypass ----------
def test_the_agents_cannot_reach_the_graph_except_over_mcp():
    banned = ("app.tools", "app.db", "app.seed", "falkordb", "redis")
    pkg = Path(agents_pkg.__file__).parent
    for f in pkg.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                   [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not [m for m in mods if m.startswith(banned)], f"{f.name} imports {mods}"
    from app.agents.gateway import TOOLS
    assert all(hasattr(t, "mcp_tool") and not hasattr(t, "run") for t in TOOLS.values())


def test_servers_run_in_separate_processes_without_the_llm_key(ctx, mcp, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-travel")
    c = mcp("auditor")
    env = c.server_env()
    assert "ANTHROPIC_API_KEY" not in env and "sk-should-not-travel" not in json.dumps(env)
    assert env["MCP_GRAPH_NAMES"] and "FALKORDB_PORT" in env
    c.call("find_derived", dict(user_id="user_ravi"))
    assert {r["pid"] for r in read_log(mcp.log) if r["source"] == "mcp-server:auditor"} != {os.getpid()}
