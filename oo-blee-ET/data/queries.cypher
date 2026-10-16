// Read-only exploration queries for the seeded tenant_acme graph (FalkorDB Browser or tests).
// Each query ends with ";". These are for looking around, not the Phase 2 erasure logic.
// In application code, pass values as query parameters; never interpolate user input.

// Node counts by label
MATCH (n) RETURN labels(n)[0] AS label, count(n) AS nodes ORDER BY label;

// Relationship counts by type
MATCH ()-[r]->() RETURN type(r) AS rel, count(r) AS edges ORDER BY rel;

// Facts about Ravi, oldest first (includes the superseded one)
MATCH (f:Fact)-[:ABOUT]->(:User {id: 'user_ravi'})
RETURN f.id AS id, f.tier AS tier, f.text AS text ORDER BY f.created_at;

// Everything derived (directly or through several levels) from one fact
MATCH (src:Fact {id: 'fact_ravi_trip_lisbon'})<-[:SUMMARIZES|INFERRED_FROM|BASED_ON*1..6]-(d)
RETURN DISTINCT labels(d)[0] AS label, d.id AS id ORDER BY label, id;

// Integrity check: content nodes that no Memory contains (expect zero rows)
MATCH (n) WHERE (n:Fact OR n:Summary OR n:Inference OR n:Action)
  AND NOT ((:Memory)-[:CONTAINS]->(n))
RETURN n.id AS orphan;

// Explicit cross-tenant leak edges
MATCH (f:Fact)-[:LEAKED_INTO]->(t:Tenant) RETURN f.id AS fact, t.id AS tenant;
