"""Read-only export of a tenant graph for the visualization.

The UI must show what is really in the database, so this reads the graph itself. It never writes.
Agents do not use it: they reach the graph only through MCP.
"""
from __future__ import annotations

import hashlib
import json


def export_graph(client, graph_name: str) -> dict:
    g = client.select_graph(graph_name)
    nodes = []
    for (n,) in g.ro_query("MATCH (n) RETURN n").result_set:
        props = dict(n.properties)
        props["label"] = n.labels[0]
        nodes.append(props)
    nodes.sort(key=lambda p: p["id"])
    edges = [{"id": f"{a}|{r}|{b}", "src": a, "rel": r, "dst": b}
             for a, r, b in g.ro_query("MATCH (a)-[r]->(b) RETURN a.id, type(r), b.id").result_set]
    edges.sort(key=lambda e: e["id"])
    counts_n: dict[str, int] = {}
    for n in nodes:
        counts_n[n["label"]] = counts_n.get(n["label"], 0) + 1
    counts_e: dict[str, int] = {}
    for e in edges:
        counts_e[e["rel"]] = counts_e.get(e["rel"], 0) + 1
    blob = json.dumps([nodes, edges], sort_keys=True, default=str).encode()
    return {"graph": graph_name, "nodes": nodes, "edges": edges,
            "counts": {"nodes": dict(sorted(counts_n.items())), "edges": dict(sorted(counts_e.items()))},
            "version": hashlib.sha256(blob).hexdigest()[:16]}


def list_users(client, graph_name: str) -> list[dict]:
    rows = client.select_graph(graph_name).ro_query(
        "MATCH (u:User) RETURN u.id, u.display_name ORDER BY u.id").result_set
    return [{"id": r[0], "display_name": r[1]} for r in rows]
