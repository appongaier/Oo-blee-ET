import json
from collections import defaultdict

import pytest

from app import config, db
from app.schema import DERIVATION_RELS
from app.seed import build_dataset, seed_all
from app.tools import ToolContext

TEST_NAMES = {"acme": "test_tenant_acme", "globex": "test_tenant_globex"}


@pytest.fixture(scope="session")
def falkordb_client():
    config.get_settings.cache_clear()
    if not db.check_falkordb()["reachable"]:
        pytest.skip("FalkorDB not running (docker compose up -d)")
    return db.get_client()


def _cleanup(client):
    for n in TEST_NAMES.values():
        if n in client.list_graphs():
            client.select_graph(n).delete()


@pytest.fixture
def ctx(falkordb_client, tmp_path):
    """Freshly seeded throwaway tenant graphs plus a tool context for Acme."""
    _cleanup(falkordb_client)
    seed_all(falkordb_client, graph_names=TEST_NAMES)
    yield ToolContext(falkordb_client, "acme", TEST_NAMES, receipt_dir=str(tmp_path))
    _cleanup(falkordb_client)


def snapshot(client, tenant: str) -> dict:
    """Everything in a tenant graph: nodes with all properties, and all edges."""
    g = client.select_graph(TEST_NAMES[tenant])
    nodes = {r[0].properties["id"]: (r[0].labels[0], json.dumps(r[0].properties, sort_keys=True))
             for r in g.ro_query("MATCH (n) RETURN n").result_set}
    edges = sorted(tuple(r) for r in g.ro_query("MATCH (a)-[r]->(b) RETURN a.id, type(r), b.id").result_set)
    return {"nodes": nodes, "edges": edges}


def expected_erasure(user_id: str, tenant: str = "acme") -> set[str]:
    """The deletion set computed straight from the seed dataset, independent of the tools."""
    g = build_dataset()[tenant]
    about = {e.src_id for e in g.edges if e.rel == "ABOUT" and e.dst_id == user_id}
    children = defaultdict(set)
    for e in g.edges:
        if e.rel in DERIVATION_RELS:
            children[e.dst_id].add(e.src_id)
    covered, frontier = set(about), set(about)
    while frontier:
        frontier = set().union(*(children[n] for n in frontier)) - covered
        covered |= frontier
    sources = set()
    for e in g.edges:
        if e.rel == "DERIVED_FROM" and e.src_id in about:
            users = {x.src_id for x in g.edges if x.rel == "DERIVED_FROM" and x.dst_id == e.dst_id}
            if users <= covered:
                sources.add(e.dst_id)
    memories = set()
    for e in g.edges:
        if e.rel == "CONTAINS" and e.dst_id in covered:
            held = {x.dst_id for x in g.edges if x.rel == "CONTAINS" and x.src_id == e.src_id}
            if held <= covered:
                memories.add(e.src_id)
    return covered | {user_id} | sources | memories
