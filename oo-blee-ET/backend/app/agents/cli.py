"""Try the agents from a terminal (needs ANTHROPIC_API_KEY and a seeded FalkorDB).

    python -m app.agents.cli pipeline                 ingest -> analyse -> audit, with handoffs
    python -m app.agents.cli ask analyst "What is stored about Ravi?"
    python -m app.agents.cli erase                    preview, then confirm by typing the user id
"""
import argparse
import json
import logging
import sys

from app.agents import AnthropicLLM, LLMError, Orchestrator
from app.mcp_support import AuthorizationClaim, McpToolError

DEFAULT_STATEMENTS = ["Ravi plays chess on weekends.", "Ravi is learning Portuguese.",
                      "Ravi prefers window seats on flights."]


def show(title: str, resp: dict) -> None:
    print(f"\n=== {title} ({resp['agent_id']}) ===\n{resp['response']}")
    for t in resp["tool_trace"]:
        print(f"  [{t['seq']}] {t['tool']} -> {t['status']}" + (f" ({t['error_code']})" if t["error_code"] else ""))
    for w in resp["warnings"]:
        print(f"  ! {w['code']}: {w['message']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", default="acme")
    p.add_argument("--user", default="user_ravi")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("pipeline")
    pl.add_argument("-s", "--statement", action="append", help="something the user says (repeatable)")
    ask = sub.add_parser("ask")
    ask.add_argument("role", choices=["concierge", "analyst", "auditor"])
    ask.add_argument("message")
    sub.add_parser("erase")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        llm = AnthropicLLM()
    except LLMError as exc:
        print(f"Cannot start: {exc}")
        return 1
    try:
        o = Orchestrator(args.tenant, args.user, llm)
    except McpToolError as exc:
        print(f"Cannot start: {exc.message}. Is FalkorDB running and the graph seeded? "
              "(docker compose up -d; python data\\seed.py --reset)")
        return 1

    try:
        if args.cmd == "ask":
            show(args.role, o.run(args.role, args.message))
        elif args.cmd == "pipeline":
            out = o.run_pipeline(args.statement or DEFAULT_STATEMENTS)
            for role in ("concierge", "analyst", "auditor"):
                if role in out:
                    show(role, out[role])
            print("\nhandoffs:", json.dumps(out["handoffs"], indent=2))
            if "stopped" in out:
                print("stopped:", out["stopped"])
        else:
            pv = o.preview_erasure()
            pre = pv["preview"]
            print(json.dumps({k: pre[k] for k in ("counts_by_node_type", "counts_by_edge_type", "kept_sources",
                                                  "shared_derived_nodes", "cross_tenant_findings", "can_execute")}, indent=2))
            show("preview", pv["auditor"])
            if input(f"\nType {args.user} to erase this data (anything else cancels): ").strip() != args.user:
                print("Cancelled. Nothing was deleted.")
                return 0
            auth = AuthorizationClaim(args.user, "data_subject", (args.tenant,))
            done = o.execute_erasure(pre["preview_version"], "requested from the CLI", auth)
            print("receipt:", done["receipt"]["job_id"], "deleted", len(done["receipt"]["deleted_node_ids"]), "nodes")
            show("post-erasure audit", done["auditor"])
        return 0
    finally:
        o.close()


if __name__ == "__main__":
    sys.exit(main())
