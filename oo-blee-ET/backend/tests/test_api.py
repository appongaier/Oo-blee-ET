"""The HTTP API behind the UI, against a live FalkorDB and real MCP server processes."""
import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.constants import DISCLAIMER
from app.lifecycle import Runtime, get_runtime
from app.main import app
from app.seed import graph_counts
from tests.conftest import TEST_NAMES, expected_erasure
from tests.test_agents import ScriptedLLM, _pipeline_llms, say, use

pytestmark = pytest.mark.integration
RAVI = {"principal_id": "user_ravi", "role": "data_subject"}
PRIYA = {"principal_id": "user_priya", "role": "data_subject"}
ADMIN = {"principal_id": "admin_1", "role": "tenant_admin"}
AUDITOR = {"principal_id": "agent_auditor", "role": "auditor"}


@pytest.fixture
def api(ctx, monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)
    made = []

    def make(**kw):
        rt = Runtime(graph_names=TEST_NAMES, receipt_dir=ctx.receipt_dir, log_path=f"{ctx.receipt_dir}/mcp.jsonl", **kw)
        made.append(rt)
        app.dependency_overrides[get_runtime] = lambda: rt
        return TestClient(app), rt

    yield make
    app.dependency_overrides.clear()
    for rt in made:
        rt.close()


def preview(c, who=RAVI, user="user_ravi"):
    return c.post("/api/erasure/preview", json={"tenant": "acme", "user_id": user, "acting_as": who})


def test_config_comes_from_the_database(api):
    c, _ = api()
    cfg = c.get("/api/config").json()
    by = {t["id"]: t for t in cfg["tenants"]}
    assert [u["id"] for u in by["acme"]["users"]] == ["user_priya", "user_ravi"] and by["globex"]["users"] == []
    assert by["acme"]["seeded"] and cfg["llm_available"] is False and cfg["disclaimer"] == DISCLAIMER


def test_graph_export_matches_the_database_and_versions_change(ctx, api):
    c, _ = api()
    g = c.get("/api/graph/acme").json()
    live = graph_counts(ctx.client.select_graph(TEST_NAMES["acme"]))
    assert g["counts"]["nodes"] == live["nodes"] and g["counts"]["edges"] == live["edges"]
    assert {n["id"] for n in g["nodes"]} >= {"user_ravi", "sum_team_q4", "fact_ravi_trip_lisbon"}
    assert all({"id", "src", "rel", "dst"} <= set(e) for e in g["edges"])
    assert c.get("/api/graph/acme").json()["version"] == g["version"]
    c.post("/api/memory", json={"tenant": "acme", "user_id": "user_ravi", "text": "Ravi likes tea."})
    assert c.get("/api/graph/acme").json()["version"] != g["version"]


def test_unknown_and_unseeded_tenants_are_clear_errors(ctx, api):
    c, _ = api()
    r = c.get("/api/graph/initech")
    assert r.status_code == 404 and r.json()["error"]["code"] == "UNKNOWN_TENANT"
    ctx.client.select_graph(TEST_NAMES["globex"]).delete()
    r = c.get("/api/graph/globex")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_SEEDED"
    assert {t["id"]: t["seeded"] for t in c.get("/api/config").json()["tenants"]}["globex"] is False


def test_adding_a_memory_goes_through_the_concierge_over_mcp(ctx, api):
    c, _ = api()
    r = c.post("/api/memory", json={"tenant": "acme", "user_id": "user_priya", "text": "Priya likes pottery."})
    assert r.status_code == 200
    out = r.json()
    fid = out["result"]["fact_id"]
    assert out["result"]["agent_id"] == "agent_concierge" and out["cypher"] and out["graph"]["edges"]
    ids = {n["id"] for n in c.get("/api/graph/acme").json()["nodes"]}
    assert fid in ids
    assert c.post("/api/memory", json={"tenant": "acme", "user_id": "user_nobody", "text": "x"}).status_code == 404
    assert c.post("/api/memory", json={"tenant": "acme", "user_id": "user_ravi", "text": "x", "tier": "semantic"}).status_code == 422
    assert c.post("/api/memory", json={"tenant": "acme", "user_id": "user_ravi", "text": ""}).status_code == 422


def test_node_details_use_the_graph_paths(api):
    c, _ = api()
    ex = c.get("/api/nodes/acme/sum_team_q4/explain").json()
    assert ex["result"]["supported"] and ex["result"]["sources"] == ["src_meeting_team_001"] and ex["cypher"]
    tr = c.get("/api/nodes/acme/src_meeting_team_001/trace").json()
    assert "sum_team_q4" in tr["result"]["downstream"]["node_ids"]
    assert c.get("/api/nodes/acme/mem_concierge_ravi/explain").status_code == 422
    assert c.get("/api/nodes/acme/nope/trace").status_code == 404


def test_preview_is_read_only_and_authorized(api):
    c, _ = api()
    before = c.get("/api/graph/acme").json()["version"]
    r = preview(c)
    p = r.json()["result"]["preview"]
    assert r.status_code == 200 and set(p["node_ids"]) == expected_erasure("user_ravi")
    assert r.json()["result"]["can_execute"] is True
    assert r.json()["result"]["mutated"] is False
    assert c.get("/api/graph/acme").json()["version"] == before
    assert preview(c, PRIYA).status_code == 403 and preview(c, PRIYA).json()["error"]["code"] == "FORBIDDEN_SUBJECT"
    assert preview(c, AUDITOR).status_code == 200 and preview(c, ADMIN).status_code == 200
    assert preview(c, ADMIN, user="user_nobody").status_code == 404


def test_full_erasure_lifecycle_over_http(api):
    c, _ = api()
    pv = preview(c).json()["result"]["preview"]
    body = {"tenant": "acme", "user_id": "user_ravi", "preview_version": pv["preview_version"], "reason": "user request"}
    for who, status, code in ((AUDITOR, 403, "FORBIDDEN_ROLE"), (PRIYA, 403, "FORBIDDEN_SUBJECT")):
        r = c.post("/api/erasure/execute", json={**body, "acting_as": who})
        assert (r.status_code, r.json()["error"]["code"]) == (status, code)
    stale = c.post("/api/erasure/execute", json={**body, "preview_version": "0" * 64, "acting_as": RAVI})
    assert stale.status_code == 409 and stale.json()["error"]["details"]["new_preview"]["preview_version"] == pv["preview_version"]
    assert c.get("/api/graph/acme").json()["nodes"]  # nothing deleted so far
    before_ids = {n["id"] for n in c.get("/api/graph/acme").json()["nodes"]}
    assert expected_erasure("user_ravi") <= before_ids

    done = c.post("/api/erasure/execute", json={**body, "acting_as": RAVI})
    assert done.status_code == 200
    receipt = done.json()["result"]["receipt"]
    after_ids = {n["id"] for n in c.get("/api/graph/acme").json()["nodes"]}
    assert set(receipt["deleted_node_ids"]) == before_ids - after_ids == expected_erasure("user_ravi")
    assert receipt["audit_result"]["graph_audit_passed"] and "fact_priya_email_pref" in after_ids

    aud = c.post("/api/audit", json={"tenant": "acme", "user_id": "user_ravi",
                                     "deleted_node_ids": receipt["deleted_node_ids"], "text_terms": ["Ravi"]}).json()
    assert aud["result"]["graph_audit_passed"] and aud["result"]["covered_paths_remaining"] == 0
    assert "does not prove" in aud["result"]["limitations"]
    leak = c.post("/api/leaks", json={"tenant_a": "acme", "tenant_b": "globex"}).json()
    assert [f["status"] for f in leak["result"]["findings"]] == ["undeclared_copy"]
    assert leak["result"]["remediation_preview"]["executed"] is False
    assert any(n["id"] == "fact_globex_copy_lisbon" for n in c.get("/api/graph/globex").json()["nodes"])


def test_leak_check_before_erasure_finds_the_declared_copy(api):
    c, _ = api()
    f = c.post("/api/leaks", json={"tenant_a": "acme", "tenant_b": "globex"}).json()["result"]["findings"]
    assert [(x["source_fact_id"], x["status"]) for x in f] == [("fact_ravi_trip_lisbon", "copy_found")]


def test_demo_reset_restores_the_seed_and_can_be_turned_off(api):
    c, rt = api()
    pv = preview(c).json()["result"]["preview"]
    c.post("/api/erasure/execute", json={"tenant": "acme", "user_id": "user_ravi", "reason": "r", "acting_as": RAVI,
                                         "preview_version": pv["preview_version"]})
    assert "user_ravi" not in {n["id"] for n in c.get("/api/graph/acme").json()["nodes"]}
    out = c.post("/api/demo/reset").json()
    assert out["graphs"]["acme"]["nodes"]["User"] == 2
    assert "user_ravi" in {n["id"] for n in c.get("/api/graph/acme").json()["nodes"]}
    c2, _ = api(demo_controls=False)
    r = c2.post("/api/demo/reset")
    assert r.status_code == 403 and r.json()["error"]["code"] == "DEMO_CONTROLS_OFF"


def test_agent_endpoints_need_a_model_and_return_the_agent_response_shape(api):
    c, _ = api()
    r = c.post("/api/agents/ask", json={"tenant": "acme", "user_id": "user_ravi", "role": "auditor", "message": "hi"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "LLM_NOT_CONFIGURED"

    llm = ScriptedLLM([use("explain_belief", node_id="sum_team_q4")], [say("It rests on [fact_ravi_owns_onboarding].")])
    c, _ = api(llm_factory=lambda: llm)
    assert c.get("/api/config").json()["llm_available"] is True
    r = c.post("/api/agents/ask", json={"tenant": "acme", "user_id": "user_ravi", "role": "auditor", "message": "Why?"})
    resp = r.json()["response"]
    assert r.status_code == 200 and set(resp) == {"agent_id", "response", "path", "cypher", "tool_trace", "warnings"}
    assert resp["path"] and resp["warnings"] == []

    c, _ = api(llm_factory=_pipeline_llms)
    r = c.post("/api/agents/pipeline", json={"tenant": "acme", "user_id": "user_ravi",
                                             "statements": ["Ravi plays chess on weekends.", "Ravi is learning Portuguese."]})
    out = r.json()
    assert r.status_code == 200 and [h["to"] for h in out["handoffs"]] == ["agent_analyst", "agent_auditor"]


def test_health_still_works_and_cors_allows_the_ui_origin(api):
    c, _ = api()
    assert c.get("/health").json()["status"] == "ok"
    r = c.options("/api/memory", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
