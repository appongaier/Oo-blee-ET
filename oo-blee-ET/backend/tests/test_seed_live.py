"""Seed loader and queries.cypher against a real FalkorDB server (skipped if none running)."""
from collections import Counter, defaultdict
from pathlib import Path

import pytest
from redis.exceptions import ResponseError

from app.schema import DERIVATION_RELS
from app.seed import build_dataset, graph_counts, seed_all

pytestmark = pytest.mark.integration
NAMES = {"acme": "test_tenant_acme", "globex": "test_tenant_globex"}
QUERIES = Path(__file__).resolve().parents[2] / "data" / "queries.cypher"


@pytest.fixture(scope="module")
def seeded(falkordb_client):
    def cleanup():
        for n in NAMES.values():
            if n in falkordb_client.list_graphs():
                falkordb_client.select_graph(n).delete()

    cleanup()
    seed_all(falkordb_client, graph_names=NAMES)
    yield falkordb_client
    cleanup()


def expected_counts(tenant):
    g = build_dataset()[tenant]
    return Counter(l for l, _ in g.nodes), Counter(e.rel for e in g.edges)


@pytest.mark.parametrize("tenant", NAMES)
def test_graph_matches_dataset(seeded, tenant):
    nodes, edges = expected_counts(tenant)
    got = graph_counts(seeded.select_graph(NAMES[tenant]))
    assert got["nodes"] == dict(nodes) and got["edges"] == dict(edges)


def test_reseeding_is_idempotent(seeded):
    before = graph_counts(seeded.select_graph(NAMES["acme"]))
    seed_all(seeded, graph_names=NAMES)
    assert graph_counts(seeded.select_graph(NAMES["acme"])) == before


def test_unique_id_constraint_rejects_duplicates(seeded):
    g = seeded.select_graph(NAMES["acme"])
    with pytest.raises(ResponseError):
        g.query("CREATE (:Fact {id: 'fact_ravi_trip_lisbon'})")


def test_tenant_graphs_are_isolated(seeded):
    g = seeded.select_graph(NAMES["globex"])
    assert g.ro_query("MATCH (u:User) RETURN count(u)").result_set[0][0] == 0
    assert g.ro_query("MATCH (n {id: 'user_ravi'}) RETURN count(n)").result_set[0][0] == 0


def run_queries(graph):
    lines = [l for l in QUERIES.read_text().splitlines() if not l.startswith("//")]
    stmts = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
    return [graph.ro_query(s).result_set for s in stmts]


def test_queries_file_runs_and_integrity_check_is_clean(seeded):
    results = run_queries(seeded.select_graph(NAMES["acme"]))
    assert len(results) == 6
    assert results[4] == []  # no orphaned content nodes
    assert results[5] == [["fact_ravi_trip_lisbon", "globex"]]


def test_variable_length_traversal_matches_python_bfs(seeded):
    data = build_dataset()["acme"]
    children = defaultdict(set)
    for e in data.edges:
        if e.rel in DERIVATION_RELS:
            children[e.dst_id].add(e.src_id)
    expected, frontier = set(), {"fact_ravi_trip_lisbon"}
    while frontier:
        frontier = set().union(*(children[n] for n in frontier)) - expected
        expected |= frontier
    rows = run_queries(seeded.select_graph(NAMES["acme"]))[3]
    assert {r[1] for r in rows} == expected and len(expected) >= 5
