"""The agent loop: prompts, tool calling, evidence collection and grounding checks.

The LLM proposes tool calls; the gateway decides whether they run. Every graph-dependent claim
is checked afterwards against what the tools actually returned in this turn.
"""
from __future__ import annotations

import json
import re

from app.agents.gateway import AGENT_IDS, ToolGateway, tools_for
from app.agents.llm import LLM, LLMError

MAX_STEPS = 8


def warn(code: str, message: str, node_id: str | None = None, severity: str = "warning") -> dict:
    w = {"code": code, "severity": severity, "message": message}
    if node_id:
        w["node_id"] = node_id
    return w

MAX_PATHS = 100

SYSTEM_INSTRUCTION = (
    "You must never present a graph-dependent factual claim about a user's stored memory without "
    "retrieving its supporting graph path. If the graph cannot support the claim, explicitly say that "
    "the evidence is unavailable. Do not fabricate provenance, memories, tool results, or deletion outcomes."
)

ROLE_PROMPTS = {
    "concierge": (
        "You are the Concierge (agent_concierge). You collect synthetic information from the user and write "
        "episodic memories. Record only what the user actually said, one fact per remember call, in a short "
        "sentence. Never invent or embellish a memory. If the user's new information replaces a stored fact, "
        "read memory with find_derived, record the new fact, then call mark_superseded. Answer questions "
        "about stored memories only from tool results, citing node ids in square brackets."),
    "analyst": (
        "You are the Analyst (agent_analyst). You read the memories the Concierge wrote and create summaries "
        "and inferences, and you may propose synthetic actions. Read first with find_derived. A summary must "
        "only restate its sources. An inference must follow from the facts you cite, with an honest "
        "confidence. Propose an action only if an inference you created supports it. Every derived node must "
        "cite its parent ids in the tool call. Cite node ids in square brackets in your answer."),
    "auditor": (
        "You are the Auditor (agent_auditor). You read the graph, explain provenance, run audits and preview "
        "erasures. You never create memories and you cannot erase anything: forget_preview only shows what "
        "would be deleted. Report results exactly as the tools return them, including warnings. A graph audit "
        "does not prove deletion from logs, backups, caches or model-provider storage; say so when "
        "reporting audit results. Cite node ids in square brackets."),
}
_COMMON = ("Use the tools to read the graph before answering. Keep answers short and plain. If a tool "
           "returns an error, say what failed; do not pretend it worked.")

_ID_RE = re.compile(r"\b(?:fact|sum|inf|act|mem|src|user)_[A-Za-z0-9_]+")
_KEEP = ("id", "label", "text", "tier", "type", "status", "confidence", "agent_id", "sensitivity", "created_at")


def system_prompt(role: str, gateway: ToolGateway) -> str:
    s = gateway.session
    who = f"{s.subject_display_name} ({s.subject_user_id})" if s.subject_display_name else s.subject_user_id
    return (f"{SYSTEM_INSTRUCTION}\n\n{ROLE_PROMPTS[role]}\n\n{_COMMON}\n\n"
            f"Session: tenant '{s.tenant_id}', subject user {who}. Synthetic demo data only.")


# ---------- tool output handling ----------
def _cap(obj, n: int = 40):
    if isinstance(obj, list):
        return [_cap(x, n) for x in obj[:n]] + ([f"... {len(obj) - n} more"] if len(obj) > n else [])
    if isinstance(obj, dict):
        return {k: _cap(v, n) for k, v in obj.items()}
    return obj


def compact(output: dict) -> str:
    """What the LLM sees: results and node properties, without Cypher or parameters."""
    graph = output["graph"]
    slim = [{k: n[k] for k in _KEEP if k in n} for n in graph.get("nodes", [])[:60]]
    body = {"result": _cap(output["result"]), "nodes": slim, "edges": _cap(graph.get("edges", []), 120),
            "warnings": [{"code": w["code"], "node_id": w.get("node_id")} for w in output["warnings"][:30]]}
    return json.dumps(body, default=str)


def evidence_ids(output: dict) -> set[str]:
    blob = json.dumps(output["result"], default=str) + " ".join(n["id"] for n in output["graph"].get("nodes", []))
    return set(_ID_RE.findall(blob))


def extract_paths(tool: str, output: dict) -> list[dict]:
    r, g = output["result"], output["graph"]
    tag = lambda nodes, edges: {"source_tool": tool, "nodes": nodes, "edges": edges}  # noqa: E731
    if tool == "explain_belief":
        return [tag(p["nodes"], p["edges"]) for p in r["paths"]]
    if tool == "trace_provenance":
        return [tag(p["nodes"], p["edges"]) for d in ("upstream", "downstream") for p in r[d]["paths"]]
    if tool == "find_derived":
        types = {n["id"]: n["label"] for n in g["nodes"]}
        pairs = {(e["src"], e["dst"]): e["rel"] for e in g["edges"]}
        out = []
        for d in r["derived"]:
            for ids in d["paths"]:
                edges = []
                for a, b in zip(ids, ids[1:]):
                    if (b, a) in pairs:
                        edges.append({"src": b, "rel": pairs[(b, a)], "dst": a})
                out.append(tag([{"id": i, "type": types.get(i)} for i in ids], edges))
        return out
    if "edges_created" in r:
        ids = []
        for e in r["edges_created"]:
            ids += [e["src"], e["dst"]]
        return [tag([{"id": i} for i in dict.fromkeys(ids)], r["edges_created"])]
    if tool == "mark_superseded":
        e = r["edge"]
        return [tag([{"id": e["src"]}, {"id": e["dst"]}], [e])]
    return []


def _created(output: dict) -> dict:
    return {k: output["result"][k] for k in ("fact_id", "summary_id", "inference_id", "action_id")
            if k in output["result"]}


# ---------- grounding ----------
def ground(text: str, evidence: set[str], supplied: set[str], display_name: str) -> tuple[str, list[dict]]:
    """Check a final answer against what was retrieved. Edits nothing silently: it adds a visible
    note and a warning. This is a safety net, not proof; the prompt and the gateway do the real work."""
    warnings = []
    cited = set(_ID_RE.findall(text))
    unsupported = sorted(cited - evidence - supplied)
    if unsupported:
        warnings.append(warn("UNSUPPORTED_CITATION", f"cited ids that no tool returned this turn: {unsupported}"))
        text += f"\n\n(Note: these ids were not retrieved from the graph in this turn: {', '.join(unsupported)}.)"
    mentions_user = bool(display_name) and re.search(rf"\b{re.escape(display_name)}\b", text, re.I)
    if not evidence and (cited or mentions_user):
        warnings.append(warn("NO_GRAPH_EVIDENCE", "the answer refers to stored memory but no graph path was retrieved"))
        text = "Evidence unavailable: no graph path was retrieved for this answer.\n\n" + text
    return text, warnings


# ---------- the loop ----------
def run_agent(role: str, gateway: ToolGateway, llm: LLM, user_message: str, *, max_steps: int = MAX_STEPS,
              artifacts: dict | None = None) -> dict:
    """Run one agent turn. Returns {agent_id, response, path, cypher, tool_trace, warnings}.
    `artifacts`, if given, is filled with the ids of nodes this turn created (for handoffs)."""
    start = len(gateway.log)
    system = system_prompt(role, gateway)
    tools = [t.schema() for t in tools_for(role)]
    messages: list[dict] = [{"role": "user", "content": user_message}]
    paths, cypher, warnings, evidence = [], [], [], set()
    created: dict[str, list[str]] = {}
    text = ""

    for _ in range(max_steps):
        try:
            resp = llm.create(system=system, messages=messages, tools=tools)
        except LLMError as exc:
            warnings.append(warn("LLM_ERROR", str(exc), severity="blocking"))
            text = "The language model is unavailable, so I could not complete this request. Nothing was claimed."
            break
        text = resp.text
        calls = resp.tool_calls
        if not calls:
            break
        messages.append({"role": "assistant", "content": resp.blocks})
        results = []
        for call in calls:
            r = gateway.call(role, call["name"], call["input"], resp.text or None)
            if r["ok"]:
                out = r["output"]
                cypher += out["cypher"]
                evidence |= evidence_ids(out)
                paths += extract_paths(call["name"], out)
                for k, v in _created(out).items():
                    created.setdefault(k, []).append(v)
                content = compact(out)
            else:
                warnings.append(warn("TOOL_ERROR", f"{call['name']}: {r['error']['code']}"))
                content = json.dumps({"error": {"code": r["error"]["code"], "message": r["error"]["message"]}})
            results.append({"type": "tool_result", "tool_use_id": call["id"], "content": content,
                            "is_error": not r["ok"]})
        messages.append({"role": "user", "content": results})
    else:
        warnings.append(warn("MAX_STEPS_REACHED", f"stopped after {max_steps} model steps"))
        text = text or "I stopped before finishing: too many tool steps."

    supplied = set(_ID_RE.findall(user_message))
    text, g = ground(text, evidence, supplied, gateway.session.subject_display_name)
    warnings += g
    uniq, seen = [], set()
    for p in paths:
        k = json.dumps(p, sort_keys=True)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    if len(uniq) > MAX_PATHS:
        warnings.append(warn("PATHS_TRUNCATED", f"{len(uniq) - MAX_PATHS} paths omitted"))
        uniq = uniq[:MAX_PATHS]
    if artifacts is not None:
        artifacts.update(created)
    return {"agent_id": AGENT_IDS[role], "response": text, "path": uniq, "cypher": cypher,
            "tool_trace": gateway.log[start:], "warnings": warnings}
