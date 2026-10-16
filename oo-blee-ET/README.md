# The Forgetting Agent

Multi-agent memory as a provenance graph in FalkorDB: explain, preview, and safely erase a user's data and everything derived from it.

> Synthetic demo data. This prototype demonstrates graph-based erasure workflows and is not legal advice or proof of complete regulatory compliance.

**Status: Phase 5 (graph UI).** Next: Phase 6, after approval.

## Run it (Windows Command Prompt)

Requires Docker Desktop, Python 3.11+, and Node 18+.

```
copy .env.example .env
docker compose up -d

cd backend
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pytest
uvicorn app.main:app --reload --port 8000
```

Seed the synthetic graphs (from the repo root, venv active):

```
python data\seed.py --reset
```

Then open FalkorDB Browser (http://localhost:3000), pick `tenant_acme`, and try the queries in `data/queries.cypher`. The graph model is in `data/graph-model.md`.

Open a second Command Prompt for the frontend:

```
cd frontend
npm install
npm run dev
```

- API health: http://localhost:8000/health
- FalkorDB health: http://localhost:8000/health/graph
- Frontend: http://localhost:5173
- FalkorDB Browser (optional): http://localhost:3000

`pytest` skips the live-database tests (149 of 182, and the full run takes about two minutes because tests start MCP server processes) when FalkorDB is not running. Run `docker compose up -d` first to run them all.

The tools are plain Python functions for now (`from app.tools import forget, ...`); the model, policy and transaction notes are in `data/graph-model.md`.

## Open the UI

With FalkorDB running and seeded (above), start the API from `backend` (`uvicorn app.main:app --port 8000`), then
either:

```
cd frontend
npm install
npm run dev
```

and open http://localhost:5173 (Vite forwards `/api` to port 8000), or build it once and let the API serve it:

```
npm run build
```

and open http://localhost:8000. Frontend tests: `npm test`. The "Agents" tab needs `ANTHROPIC_API_KEY`; the rest
works without it. "Reset demo data" in the header reseeds the synthetic graphs.

## Try the agents

Needs a real key. Put `ANTHROPIC_API_KEY` in `.env`, start FalkorDB, seed it, then from `backend`:

```
python -m app.agents.cli pipeline
python -m app.agents.cli ask auditor "Why does sum_team_q4 exist?"
python -m app.agents.cli erase
```

`erase` shows a preview and only deletes after you type the user id. Every tool call and handoff is written to `backend/logs/mcp.jsonl`; the MCP server itself is `mcp/server.py` (see `data/graph-model.md`). The tests use a scripted model, so
the live Anthropic API has not been exercised yet; check `LLM_MODEL` exists on your account.

## Layout

| Path | Purpose |
|---|---|
| `backend/` | FastAPI app, tenant registry, schema, seed loader, `app/tools/` (the 11 graph tools), tests |
| `frontend/` | Vite + React + TypeScript + Cytoscape.js: the graph UI |
| `data/` | `seed.py` (CLI), `queries.cypher` (exploration queries), `graph-model.md` |
| `mcp/` | `server.py`, the MCP server (one process per role). Do not add `__init__.py`; it would shadow the MCP SDK |
| `docker-compose.yml` | One FalkorDB server; tenants become separate graphs inside it |

## Notes

- The database ports are bound to localhost only.
- Pin `falkordb/falkordb` to a tested tag before the final demo.
- Never commit `.env`.
