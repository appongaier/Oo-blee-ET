"""Structural checks on the seed dataset. No database needed."""
import re
from collections import Counter, defaultdict

import pytest

from app.schema import (ALLOWED_PATTERNS, DERIVATION_RELS, NODE_LABELS, SENSITIVITIES,
                        SOURCE_TYPES, TENANT_SCOPED_LABELS, TIERS)
from app.seed import build_dataset

DATA = build_dataset()
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
CONTENT = ("Fact", "Summary", "Inference", "Action")


def nodes(g, label):
    return {p["id"]: p for l, p in g.nodes if l == label}


def out_edges(g, src_id, rel):
    return [e for e in g.edges if e.src_id == src_id and e.rel == rel]


def in_edges(g, dst_id, rel):
    return [e for e in g.edges if e.dst_id == dst_id and e.rel == rel]


@pytest.mark.parametrize("tenant", DATA)
def test_ids_unique_and_edges_resolve(tenant):
    g = DATA[tenant]
    ids = [p["id"] for _, p in g.nodes]
    assert len(ids) == len(set(ids))
    known = {p["id"]: l for l, p in g.nodes}
    for e in g.edges:
        assert known[e.src_id] == e.src_label and known[e.dst_id] == e.dst_label
        assert (e.src_label, e.rel, e.dst_label) in ALLOWED_PATTERNS
    assert len(g.edges) == len(set(g.edges))


@pytest.mark.parametrize("tenant", DATA)
def test_enums_timestamps_and_tenant_ids(tenant):
    g = DATA[tenant]
    for label, p in g.nodes:
        assert label in NODE_LABELS
        if "tier" in p:
            assert p["tier"] in TIERS
        if "sensitivity" in p:
            assert p["sensitivity"] in SENSITIVITIES
        if "created_at" in p:
            assert ISO.match(p["created_at"])
        if label in TENANT_SCOPED_LABELS:
            assert p["tenant_id"] == tenant
    for p in nodes(g, "Source").values():
        assert p["type"] in SOURCE_TYPES
    for p in nodes(g, "Inference").values():
        assert 0 <= p["confidence"] <= 1


@pytest.mark.parametrize("tenant", DATA)
def test_derived_nodes_have_parents(tenant):
    g = DATA[tenant]
    for label, rel in (("Summary", "SUMMARIZES"), ("Inference", "INFERRED_FROM"),
                       ("Action", "BASED_ON")):
        for nid in nodes(g, label):
            assert out_edges(g, nid, rel), f"{nid} has no {rel} parent"
    for nid in nodes(g, "Fact"):
        assert len(out_edges(g, nid, "DERIVED_FROM")) == 1


@pytest.mark.parametrize("tenant", DATA)
def test_content_nodes_traceable_to_one_authoring_agent(tenant):
    g = DATA[tenant]
    mems = nodes(g, "Memory")
    for label in CONTENT:
        for nid, p in nodes(g, label).items():
            holders = in_edges(g, nid, "CONTAINS")
            assert len(holders) == 1, nid
            mem = mems[holders[0].src_id]
            writers = in_edges(g, mem["id"], "WROTE")
            assert len(writers) == 1
            assert writers[0].src_id == p["agent_id"] == mem["agent_id"]


def test_every_acme_fact_has_exactly_one_subject():
    g = DATA["acme"]
    for nid in nodes(g, "Fact"):
        assert len(out_edges(g, nid, "ABOUT")) == 1


def test_globex_copy_is_the_only_fact_without_subject_and_points_back_to_acme():
    g = DATA["globex"]
    (fid,) = nodes(g, "Fact")
    assert not out_edges(g, fid, "ABOUT")
    src = nodes(g, "Source")[out_edges(g, fid, "DERIVED_FROM")[0].dst_id]
    assert src["type"] == "cross_tenant_copy"
    tenant, fact_id = src["url_or_session"].split(":")
    assert tenant == "tenant_acme" and fact_id in nodes(DATA["acme"], "Fact")
    leaks = [e for e in DATA["acme"].edges if e.rel == "LEAKED_INTO"]
    assert [(e.src_id, e.dst_id) for e in leaks] == [(fact_id, "globex")]


def test_hard_cases_are_present():
    g = DATA["acme"]
    subject = {e.src_id: e.dst_id for e in g.edges if e.rel == "ABOUT"}
    # shared source used by facts about two users
    users_by_source = defaultdict(set)
    for e in g.edges:
        if e.rel == "DERIVED_FROM":
            users_by_source[e.dst_id].add(subject[e.src_id])
    assert any(len(u) == 2 for u in users_by_source.values())
    # a summary whose parents are facts about two users
    parents = defaultdict(set)
    for e in g.edges:
        if e.rel == "SUMMARIZES" and e.dst_label == "Fact":
            parents[e.src_id].add(subject[e.dst_id])
    assert any(len(u) == 2 for u in parents.values())
    # multi-level derivation and a superseded fact that is still about its user
    assert any(e.rel == "SUMMARIZES" and e.dst_label == "Summary" for e in g.edges)
    assert any(e.rel == "INFERRED_FROM" and e.dst_label == "Summary" for e in g.edges)
    assert Counter(e.rel for e in g.edges)["SUPERSEDED_BY"] == 1
    # all three tiers are represented
    tiers = {p["tier"] for _, p in g.nodes if "tier" in p}
    assert tiers == set(TIERS)
    assert set(DERIVATION_RELS) <= {e.rel for e in g.edges}
