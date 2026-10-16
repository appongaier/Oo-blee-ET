# Graph model and erasure policy

Synthetic data only. One FalkorDB server, one graph per tenant: `tenant_acme`, `tenant_globex`.
Edges cannot cross graphs, so cross-tenant links are modeled explicitly (see Leaks).

## Direction rule

A derived node points at its parent: `(Summary)-[:SUMMARIZES]->(Fact)`.
"Downstream" of a node = everything that reaches it by following derivation edges in reverse.
Every node has a unique `id` (index plus unique constraint per label). Timestamps are
ISO-8601 UTC strings (`2026-10-01T09:00:00Z`).

## Nodes

| Label | Properties |
|---|---|
| Tenant | id, name |
| User | id, tenant_id, display_name |
| Agent | id, role |
| Source | id, type (`chat_session`, `meeting_transcript`, `cross_tenant_copy`), url_or_session, created_at |
| Fact | id, text, tier, sensitivity (`low`/`medium`/`high`), created_at, agent_id, tenant_id |
| Summary | id, text, tier, created_at, agent_id, tenant_id |
| Inference | id, text, **tier**, confidence, created_at, agent_id, tenant_id |
| Action | id, type, status, created_at, agent_id, tenant_id |
| Memory | id, tier, agent_id, session_id, tenant_id |

Tiers: `episodic`, `semantic`, `procedural`.
Deviation from the brief: Inference gets a `tier` property because Tool 3 says to record it.

## Relationships and erasure semantics

| Pattern | Meaning | Erasure semantics |
|---|---|---|
| (Fact)-[:DERIVED_FROM]->(Source) | where the fact came from | Provenance only. Source is audit metadata: removed only if no other fact still uses it |
| (Fact)-[:ABOUT]->(User) | the fact's subject | Defines user-originated personal data (category A) |
| (Summary)-[:SUMMARIZES]->(Fact or Summary) | derivation | Derived personal data (B) if any parent is covered |
| (Inference)-[:INFERRED_FROM]->(Fact or Summary) | derivation | Same as above |
| (Action)-[:BASED_ON]->(Inference) | derivation | Same as above |
| (Agent)-[:WROTE]->(Memory) | authorship of the container | Never a reason to delete. Agent nodes are kept |
| (Memory)-[:CONTAINS]->(Fact, Summary, Inference) | holds the node | Containment is not origin. Edge goes when either end goes. A Memory left empty is cleaned up |
| (Fact)-[:SUPERSEDED_BY]->(Fact) | history | Does not exempt the old fact from erasure |
| (Fact)-[:LEAKED_INTO]->(Tenant) | marks a copy in another tenant | Never auto-deleted. Needs a separate authorized remediation |

**Added in Phase 1 (one relationship):** `(Memory)-[:CONTAINS]->(Action)`. Without it an Action
has no traceable author (invariant 4). Same containment semantics as above.

**Generalized targets (no new types):** SUMMARIZES may target a Summary, and INFERRED_FROM may
target a Summary, so derivations can chain through several levels.

Allowed patterns are enforced in code (`backend/app/schema.py`, `ALLOWED_PATTERNS`).

## Seed data

- **Acme:** users Ravi and Priya; three agents; sources for each chat plus one shared team
  meeting; 9 facts, 4 summaries, 3 inferences, 3 actions.
- **Hard cases on purpose:**
  - a superseded fact (Ravi: Pune -> Bengaluru) that is still ABOUT Ravi;
  - a summary of a summary, and an inference drawn from a summary;
  - a shared Source and a shared Summary (`sum_team_q4`) that mix a Ravi fact and a Priya fact;
  - Priya's own facts, summary, inference and action, which must survive Ravi's erasure;
  - a procedural fact written by the Analyst.
- **Leak:** `fact_ravi_trip_lisbon` has `LEAKED_INTO` -> Tenant `globex`. In `tenant_globex`,
  `fact_globex_copy_lisbon` is the copy, written by the Analyst. Its Source
  (`cross_tenant_copy`) records `tenant_acme:fact_ravi_trip_lisbon`.
  The copy has no ABOUT edge on purpose: its subject can only be resolved through that
  reference. Later phases must report it as a finding, not guess a provenance link.

## Erasure policy (Phase 2)

| Category | What it covers | Treatment |
|---|---|---|
| A. user-originated | Facts ABOUT the user (superseded ones too) and the User node | Deleted |
| B. derived | Any Summary, Inference or Action with at least one covered ancestor, at any depth up to the limit | Deleted. One covered parent is enough, so a node that mixes two users' data (`sum_team_q4`) is deleted, not rewritten. The preview flags it as shared |
| C. independent | Everything else, including other users' facts and anything merely nearby | Kept |
| D. metadata | Source, Memory, Agent, Tenant | A Source is deleted only if no surviving fact uses it. A Memory only if it ends up empty. Agents and Tenants are never deleted. Receipts hold ids and types, never content |

Fail closed: deletion is refused if a traversal hit the depth limit, or a covered node has the wrong
`tenant_id`. Other provenance gaps (missing source or subject, facts with no subject, orphans) are
reported as warnings and do not block.

Cross-tenant: a `LEAKED_INTO` edge is reported in the preview and the receipt. The copy in the other
tenant is never deleted by `forget`. `detect_leak` only previews a remediation.

## Tools (`backend/app/tools/`)

Every tool takes a `ToolContext(client, tenant_id)` first and returns
`{"result", "cypher", "params", "graph", "warnings"}`; `cypher` and `params` are parallel lists of
every statement executed. Failures raise `ToolError(code, message)`.

Deviations from the brief's signatures:
- a tenant is always explicit (graphs are per tenant; ids are only unique inside one graph);
- `summarize` and `infer` take the content `text` and `infer` a required `confidence`, because the
  tools never write content themselves; both also accept earlier summaries as sources;
- `act` accepts one or several inference ids; `remember` takes optional `sensitivity` and `session_id`;
- `forget` and `detect_leak` take an `Authorization`; `audit_forgotten` takes optional
  `deleted_node_ids`, `text_terms` and `tenant_scope`.

| Tool | Notes |
|---|---|
| remember | Fact + Source + Memory + edges in one statement. Reuses the Source and Memory of a session |
| summarize, infer, act | One guarded statement: if any parent is missing, wrong-typed or in another tenant, nothing is created. Only the Analyst may call them. Actions are only ever `proposed` |
| mark_superseded | Same single subject, no cycles, one successor. The old fact stays covered by erasure |
| trace_provenance | Fact, Summary, Inference, Action, Source or User. Paths carry node types and edge directions |
| explain_belief | Built only from graph paths. If no complete path to a Source exists it says the evidence is unavailable |
| find_derived | Policy A and B above, plus which derived nodes depend on other users |
| forget | `dry_run=True` never writes. Confirming needs the preview's `preview_version`, a reason, and an authorization |
| audit_forgotten | Walks upstream from every node (the plan walks downstream), then scans for orphans and, if given terms, text mentions |
| detect_leak | Reads both tenants' graphs with explicit authorization for both. Read-only |

Depth limit: 8 hops, set per context. Each walk asks for one hop more so a truncated result is
distinguishable from a complete one.

Warning codes: `TRUNCATED_TRAVERSAL` and `TENANT_MISMATCH` are blocking. Others: `MISSING_SOURCE`,
`MISSING_SUBJECT`, `AMBIGUOUS_SUBJECT`, `MISSING_PARENT`, `MISSING_CONTAINER`, `UNATTRIBUTED_FACT`,
`SHARED_DERIVED_NODE`, `CROSS_TENANT_LEAK`, `SUPERSEDED_PARENT`, `ORPHANED_DERIVED_NODE`,
`ORPHANED_SOURCE`, `EMPTY_MEMORY`, `POSSIBLE_TEXT_MENTION`, `CROSS_TENANT_COPY_REMAINS`.

### Authorization

A check on supplied claims; the prototype has no authentication. Previews: the data subject (own data),
a tenant admin, or an auditor. Confirmed erasure: the data subject (own data) or a tenant admin.
Never the Auditor. `detect_leak` and cross-tenant audits need admin or auditor rights over both tenants.

### Transactions

- Each write is one Cypher statement. Deletion is one statement that re-checks, as it runs, that every
  planned node still exists, that no outside node gained a derivation, source or subject edge into the
  set, and that nothing new was added to a Memory being deleted. If any check fails it deletes nothing.
- There are no multi-statement transactions, so none are claimed.
- On the FalkorDB graph module tested (4.18.3), a statement that failed after a DELETE left the graph
  unchanged. That is an observation, not a documented guarantee; re-check it when pinning a version.
- Anything that slips in between preview and deletion shows up in the audit as an orphan.

### Receipts

JSON, one file per job in `RECEIPT_DIR` (default `receipts/`, git-ignored). Ids, types and counts only.
They are not signed or tamper-proof and say so.

### Audit limits

A graph audit covers the listed FalkorDB graphs only. It does not prove deletion from logs, backups,
caches, search indexes or model-provider storage, and is not legal compliance. A text-mention match is
a heuristic.

## Agents (Phase 3)

Code: `backend/app/agents/`. Three agents (Concierge, Analyst, Auditor) run a tool-calling loop on a real
LLM (Anthropic SDK; `LLM_MODEL`, default `claude-sonnet-5-5`). The LLM only proposes tool calls. A gateway
decides whether they run, and the graph tools stay the single source of truth.

| Agent | Tools it can see |
|---|---|
| Concierge | `remember` (episodic only), `mark_superseded`, `find_derived`, `explain_belief` |
| Analyst | `find_derived`, `explain_belief`, `trace_provenance`, `summarize`, `infer`, `act` |
| Auditor | `find_derived`, `explain_belief`, `trace_provenance`, `audit_forgotten`, `detect_leak`, `forget_preview` |

Enforced in code (the gateway, and again by the MCP server), not just in prompts:
- an agent can only call tools on its own list; anything else is rejected and logged;
- tenant, agent id and subject user are injected. The model cannot pass them, and unknown arguments are
  rejected, so it cannot send `dry_run`, `preview_version`, `reason` or another agent's id;
- no LLM-facing tool performs a confirmed erasure. `forget_preview` is a dry run only;
- erasure is a separate step (`Orchestrator.execute_erasure`) that needs an authorized human or service,
  a matching preview version and a reason, and is validated by the deterministic policy.

Every agent response is `{agent_id, response, path, cypher, tool_trace, warnings}`:
- `path`: provenance paths and edges returned by this turn's tool calls;
- `cypher`: every statement the successful tool calls executed;
- `tool_trace`: one entry per attempt (allowed or not) with the model's stated reason when it gave one.
  Personal text arguments are logged as `<N chars>`, never in full.

Grounding checks after each answer (a safety net, not proof):
- ids cited in the answer that no tool returned this turn (and the caller did not supply) are flagged
  `UNSUPPORTED_CITATION` and noted in the answer;
- an answer that refers to the user or to stored ids, when no tool returned graph evidence, is prefixed
  "Evidence unavailable" and flagged `NO_GRAPH_EVIDENCE`;
- tool errors are passed to the model as errors and surface as `TOOL_ERROR` warnings.

Handoffs (Concierge -> Analyst -> Auditor) carry ids taken from tool results, never model-written text,
and are logged with sequence, sender, receiver, reason and node ids. If an agent produces nothing, the
pipeline stops instead of passing on invented input.

Since Phase 4 every graph call goes over MCP (below). The Concierge reads memory through `find_derived`
(no twelfth tool was added).

## MCP (Phase 4)

Server: `mcp/server.py`, built on the official MCP Python SDK (`mcp` 2.x, stdio transport). Client:
`backend/app/agents/mcp_client.py`. The agents' package imports no tool code and holds no database
connection (a test scans for this); the only path to the graph is an MCP call.

**One server process per role**, started lazily by the orchestrator with a fixed binding
(`--role --tenant --subject`). An agent's identity, tenant and subject user are set when the process is
launched, never by tool arguments.

| Role | Tools exposed |
|---|---|
| concierge | `remember` (episodic only), `mark_superseded`, `find_derived`, `explain_belief` |
| analyst | `find_derived`, `explain_belief`, `trace_provenance`, `summarize`, `infer`, `act` |
| auditor | `find_derived`, `explain_belief`, `trace_provenance`, `audit_forgotten`, `detect_leak`, `forget` (dry run only) |
| operator | `find_derived`, `forget` (preview or confirmed erasure). Only the orchestrator's deterministic steps use it; no LLM can reach it |

Together the four roles expose all eleven tools. A tool outside a role's list is "not exposed" and the
attempt is logged. Signatures follow the brief, minus what the binding supplies (tenant, agent id).

- **Input validation:** each tool publishes a JSON schema (types, enums, id patterns, length and range
  limits). The server validates every call against it and rejects unknown arguments, so the Auditor's
  `forget` schema has no `preview_version`, `reason` or `authorization` at all.
- **Structured output:** success is `{"result","cypher","params","graph","warnings"}`; failure is
  `{"error":{"code","message","details"}}` with `isError` set. Unexpected exceptions return a generic
  `INTERNAL_ERROR`, never internals.
- **Authorization:** the Auditor server builds its own auditor authorization and refuses
  `dry_run=false` (`FORBIDDEN_DESTRUCTIVE`). Confirmed erasure only works on the operator server and needs a
  claim (data subject for their own data, or tenant admin), a matching preview version and a reason; the
  erasure policy still validates everything. Claims are not authenticated: the trust boundary is who can
  start or connect to the operator server.
- **Least privilege:** the server process gets only the FalkorDB settings, graph names, receipt folder and
  log path. The LLM API key is never passed to it.

### Audit log

JSON Lines, default `backend/logs/mcp.jsonl` (`MCP_LOG_PATH`; empty turns it off). One run id ties together
the orchestrator and every server process. Events:
- `server_start` (role, tenant, tools exposed);
- `tool_selected` (orchestrator: trace id, agent, tool, the model's stated reason, argument names);
- `tool_call` (server: same trace id, status `ok`/`error`/`rejected`, error code, timing, statement and
  warning counts) and `tool_rejected` (gateway refusals);
- `agent_turn_start` / `agent_turn_end`, `operator_call`;
- `handoff` (sequence, from, to, reason, node ids).

`reconstruct_handoffs(path, run_id)` rebuilds a run's handoffs from the log alone (tested).
Events hold ids, counts and statuses. Text arguments, secrets and authorization claims are never logged.
The model's stated reason is free text: the subject's name is masked and the length capped, but other
personal details could still appear in it.

### Limits

- Tested on Linux with the SDK's own client and server. Not tried with an external MCP client, and the
  Windows launch path (subprocess plus a background event loop) has not been run.
- Each test starts server processes, so the full suite takes about two minutes.
- Four roles can mean up to four small Python processes per run.
- MCP 2.x differs from 1.x (for example `FastMCP` is now `MCPServer`), so the SDK is pinned `>=2.3,<3`.

## UI and API (Phase 5)

**API** (`backend/app/api.py`, FastAPI, JSON). Every graph *tool* call goes over MCP, like the agents':
`/api/memory` (Concierge server), `/api/erasure/preview` and `/api/erasure/execute` (operator server),
`/api/audit`, `/api/leaks`, `/api/nodes/{tenant}/{id}/explain|trace` (Auditor server). The one direct read is
`/api/graph/{tenant}` (`graph_view.py`), a read-only export so the picture shows what is really in the
database. `/api/agents/ask` and `/api/agents/pipeline` run the LLM agents and answer 503 without an API key.
`/api/demo/reset` reseeds the synthetic data (off with `DEMO_CONTROLS=false`). Errors are
`{"error": {"code", "message", "details"}}` with a fitting HTTP status.

Authorization: each erasure request carries an `acting_as` claim (principal and role). The server fills in the
tenant. The UI's "Acting as" menu lets you see the rules fire (another data subject: 403; auditor: preview
only). It is a demonstration of the rules, not authentication.

The API keeps up to eight MCP server processes open (one per role, tenant and subject) and stops them when it
shuts down (checked).

**UI** (`frontend/`, Vite, React, TypeScript, Cytoscape.js). The graph is the main surface; the panel beside
it walks the erasure lifecycle: preview, authorize, receipt and audit, other tenants.
- Layered layout computed from the data: subject, source, facts, derived (by depth), actions, other tenants.
  Arrows point to what a node came from. Memories and agents are a toggle.
- Preview marks what would go (red), what is shared with someone else (double border), and what stays
  because others still use it (dashed amber), and dims the rest.
- A confirmed erasure re-reads the graph; nodes that disappeared are blacked out, then fade.
- Selecting a node lights up its origins (blue) and dependents (green) from `trace_provenance`, and shows
  `explain_belief`. Nodes with no complete path say the evidence is unavailable.
- The Queries tab shows the Cypher behind every action. Counts and lists on screen come from API responses;
  none are hard-coded. The disclaimer is always visible.
- Without an API key everything works except the Agents tab. The Memory tab sends text to the Concierge MCP
  tools directly, with no LLM.

Limits: checked with unit tests, a production build and a real API server; not opened in a browser (none was
available where this was built), so layout and colors are unseen. Cytoscape adds about 450 kB to the bundle.
