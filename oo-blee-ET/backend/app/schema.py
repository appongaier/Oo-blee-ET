"""Graph vocabulary: labels, relationship types, allowed patterns, and index setup.

Direction convention: a derived node points at its parent (child -> parent), e.g.
(Summary)-[:SUMMARIZES]->(Fact). "Downstream" means following those edges in reverse.
Full semantics: data/graph-model.md.
"""
import time

from redis.exceptions import ResponseError

NODE_LABELS = (
    "Tenant", "User", "Agent", "Source", "Fact", "Summary", "Inference", "Action", "Memory",
)
TIERS = ("episodic", "semantic", "procedural")
SENSITIVITIES = ("low", "medium", "high")
SOURCE_TYPES = ("chat_session", "meeting_transcript", "cross_tenant_copy")

# (source label, relationship type, target label). Anything else is rejected.
ALLOWED_PATTERNS = frozenset({
    ("Fact", "DERIVED_FROM", "Source"),
    ("Fact", "ABOUT", "User"),
    ("Summary", "SUMMARIZES", "Fact"),
    ("Summary", "SUMMARIZES", "Summary"),
    ("Inference", "INFERRED_FROM", "Fact"),
    ("Inference", "INFERRED_FROM", "Summary"),
    ("Action", "BASED_ON", "Inference"),
    ("Agent", "WROTE", "Memory"),
    ("Memory", "CONTAINS", "Fact"),
    ("Memory", "CONTAINS", "Summary"),
    ("Memory", "CONTAINS", "Inference"),
    ("Memory", "CONTAINS", "Action"),  # added in Phase 1: gives Actions a traceable author
    ("Fact", "SUPERSEDED_BY", "Fact"),
    ("Fact", "LEAKED_INTO", "Tenant"),
})
REL_TYPES = tuple(sorted({rel for _, rel, _ in ALLOWED_PATTERNS}))

# Edges that mean "the child's existence depends on the parent's content".
DERIVATION_RELS = ("SUMMARIZES", "INFERRED_FROM", "BASED_ON")

# Labels whose nodes carry a tenant_id property.
TENANT_SCOPED_LABELS = ("User", "Fact", "Summary", "Inference", "Action", "Memory")


def check_pattern(src_label: str, rel: str, dst_label: str) -> None:
    if (src_label, rel, dst_label) not in ALLOWED_PATTERNS:
        raise ValueError(f"relationship pattern not allowed: ({src_label})-[:{rel}]->({dst_label})")


def ensure_schema(graph, timeout_s: float = 15.0) -> None:
    """Create an id index and a unique-id constraint for every label. Safe to re-run."""
    for label in NODE_LABELS:
        try:
            graph.create_node_range_index(label, "id")
        except ResponseError as exc:
            if "already" not in str(exc).lower():
                raise
    have = {
        c["label"]
        for c in graph.list_constraints()
        if c["type"] == "UNIQUE" and c["entitytype"] == "NODE" and c["properties"] == ["id"]
    }
    for label in NODE_LABELS:
        if label not in have:
            graph.create_node_unique_constraint(label, "id")

    deadline = time.monotonic() + timeout_s
    while True:
        statuses = {c["status"] for c in graph.list_constraints()}
        if statuses <= {"OPERATIONAL"}:
            return
        if "FAILED" in statuses or time.monotonic() > deadline:
            raise RuntimeError(f"constraints did not become operational: {sorted(statuses)}")
        time.sleep(0.1)
