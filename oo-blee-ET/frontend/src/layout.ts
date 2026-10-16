// Layered provenance layout, computed from the graph itself.
// Left to right: subject, source, fact, derived (by depth), action. Arrows point at what a node came from.
import type { GEdge, GNode } from "./types";

export interface Placement { pos: Record<string, { x: number; y: number }>; columns: { x: number; title: string }[]; width: number; height: number }

export const COL_GAP = 210;
export const ROW_GAP = 66;
const DERIVE_RELS = new Set(["SUMMARIZES", "INFERRED_FROM", "BASED_ON"]);
export const CONTEXT_LABELS = new Set(["Memory", "Agent"]);

/** Tenant nodes that are leak destinations are always shown; a home-tenant node is context. */
export function leakTargets(edges: GEdge[]): Set<string> {
  return new Set(edges.filter((e) => e.rel === "LEAKED_INTO").map((e) => e.dst));
}

export function isContext(n: GNode, targets: Set<string>): boolean {
  return CONTEXT_LABELS.has(n.label) || (n.label === "Tenant" && !targets.has(n.id));
}

export function layout(nodes: GNode[], edges: GEdge[]): Placement {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const parents = new Map<string, string[]>();   // derived node -> parent beliefs
  for (const e of edges) if (DERIVE_RELS.has(e.rel)) (parents.get(e.src) ?? parents.set(e.src, []).get(e.src)!).push(e.dst);

  // depth of Summary/Inference nodes: 1 + deepest parent (facts are depth 0)
  const depth = new Map<string, number>();
  const depthOf = (id: string, seen = new Set<string>()): number => {
    const n = byId.get(id);
    if (!n || n.label === "Fact") return 0;
    if (depth.has(id)) return depth.get(id)!;
    if (seen.has(id)) return 1;
    seen.add(id);
    const d = 1 + Math.max(0, ...(parents.get(id) ?? []).map((p) => depthOf(p, seen)));
    depth.set(id, d);
    return d;
  };
  const derived = nodes.filter((n) => n.label === "Summary" || n.label === "Inference");
  derived.forEach((n) => depthOf(n.id));
  const maxDepth = Math.max(0, ...derived.map((n) => depth.get(n.id) ?? 1));

  // columns
  const col = new Map<string, number>();
  const actionCol = 3 + maxDepth;
  for (const n of nodes) {
    if (n.label === "User") col.set(n.id, 0);
    else if (n.label === "Source") col.set(n.id, 1);
    else if (n.label === "Fact") col.set(n.id, 2);
    else if (n.label === "Summary" || n.label === "Inference") col.set(n.id, 2 + (depth.get(n.id) ?? 1));
    else if (n.label === "Action") col.set(n.id, actionCol);
  }
  const targets = leakTargets(edges);
  const tenantCol = actionCol + 1;
  for (const n of nodes) if (n.label === "Tenant" && targets.has(n.id)) col.set(n.id, tenantCol);

  // rows: facts are ordered by subject; everything else sits at the average height of what it relates to
  const subjectOf = new Map<string, string>();
  for (const e of edges) if (e.rel === "ABOUT") subjectOf.set(e.src, e.dst);
  const users = nodes.filter((n) => n.label === "User").map((n) => n.id).sort((a, b) =>
    String(byId.get(a)?.display_name ?? a).localeCompare(String(byId.get(b)?.display_name ?? b)));
  const rank = (id: string) => { const i = users.indexOf(subjectOf.get(id) ?? ""); return i < 0 ? users.length : i; };
  const facts = nodes.filter((n) => n.label === "Fact").sort((a, b) => rank(a.id) - rank(b.id) || String(a.created_at ?? "").localeCompare(String(b.created_at ?? "")) || a.id.localeCompare(b.id));
  const y = new Map<string, number>();
  let row = 0;
  let lastRank = facts.length ? rank(facts[0].id) : 0;
  for (const f of facts) {
    if (rank(f.id) !== lastRank) { row += 0.6; lastRank = rank(f.id); }   // a little air between subjects
    y.set(f.id, row++ * ROW_GAP);
  }
  const avg = (ids: string[]) => { const v = ids.filter((i) => y.has(i)).map((i) => y.get(i)!); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : undefined; };
  const factsBySource = new Map<string, string[]>();
  for (const e of edges) if (e.rel === "DERIVED_FROM") (factsBySource.get(e.dst) ?? factsBySource.set(e.dst, []).get(e.dst)!).push(e.src);
  const factsByUser = new Map<string, string[]>();
  for (const [f, u] of subjectOf) (factsByUser.get(u) ?? factsByUser.set(u, []).get(u)!).push(f);
  for (const n of nodes) {
    if (n.label === "Source") { const v = avg(factsBySource.get(n.id) ?? []); if (v !== undefined) y.set(n.id, v); }
    if (n.label === "User") { const v = avg(factsByUser.get(n.id) ?? []); if (v !== undefined) y.set(n.id, v); }
  }
  const leaked = new Map<string, string[]>();
  for (const e of edges) if (e.rel === "LEAKED_INTO") (leaked.get(e.dst) ?? leaked.set(e.dst, []).get(e.dst)!).push(e.src);
  const byColumn = (c: number) => nodes.filter((n) => col.get(n.id) === c);
  for (let c = 3; c <= actionCol; c++) {
    for (const n of byColumn(c)) y.set(n.id, avg(parents.get(n.id) ?? []) ?? 0);
  }
  for (const n of nodes) if (n.label === "Tenant" && targets.has(n.id)) y.set(n.id, avg(leaked.get(n.id) ?? []) ?? 0);
  // resolve overlaps inside each column, keeping order
  for (let c = 0; c <= tenantCol; c++) {
    const items = byColumn(c).sort((a, b) => (y.get(a.id) ?? 0) - (y.get(b.id) ?? 0) || a.id.localeCompare(b.id));
    let prev = -Infinity;
    for (const n of items) { const v = Math.max(y.get(n.id) ?? 0, prev + ROW_GAP * 0.9); y.set(n.id, v); prev = v; }
  }

  const pos: Record<string, { x: number; y: number }> = {};
  for (const n of nodes) if (col.has(n.id)) pos[n.id] = { x: col.get(n.id)! * COL_GAP, y: y.get(n.id) ?? 0 };
  const maxY = Math.max(0, ...Object.values(pos).map((p) => p.y));
  const maxX = Math.max(0, ...Object.values(pos).map((p) => p.x));

  // context lane (memories, agents, home tenant), shown only when the layer is on
  const mem = nodes.filter((n) => n.label === "Memory").sort((a, b) => a.id.localeCompare(b.id));
  const agents = nodes.filter((n) => n.label === "Agent").sort((a, b) => a.id.localeCompare(b.id));
  const homes = nodes.filter((n) => n.label === "Tenant" && !targets.has(n.id));
  const span = Math.max(maxX, 4 * COL_GAP);
  const lane = (items: GNode[], yy: number) => items.forEach((n, i) => { pos[n.id] = { x: items.length > 1 ? (span * i) / (items.length - 1) : span / 2, y: yy }; });
  lane(mem, maxY + 150);
  lane(agents, maxY + 260);
  lane(homes, maxY + 370);

  const titles: Record<number, string> = { 0: "Subject", 1: "Source", 2: "Facts" };
  for (let d = 1; d <= maxDepth; d++) titles[2 + d] = d === 1 ? "Derived" : "Derived from derived";
  titles[actionCol] = "Actions";
  if (targets.size) titles[tenantCol] = "Other tenants";
  const present = new Set([...col.values()]);
  const columns = Object.entries(titles).filter(([c]) => present.has(Number(c))).map(([c, title]) => ({ x: Number(c) * COL_GAP, title }));
  return { pos, columns, width: maxX, height: maxY };
}
