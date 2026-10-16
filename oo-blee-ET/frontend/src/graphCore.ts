// Everything that touches the Cytoscape instance. Kept free of React so it can be tested headless.
import type { Core, ElementDefinition } from "cytoscape";
import { isContext, layout, leakTargets, type Placement } from "./layout";
import type { GEdge, GNode, GraphData } from "./types";

export interface Marks { erase: Set<string>; shared: Set<string>; keep: Set<string>; leak: Set<string>; fresh: Set<string>; up: Set<string>; down: Set<string> }
export const emptyMarks = (): Marks => ({ erase: new Set(), shared: new Set(), keep: new Set(), leak: new Set(), fresh: new Set(), up: new Set(), down: new Set() });

const MARK_CLASSES = ["erase", "shared", "keep", "leak", "fresh", "up", "down", "dim"] as const;
const trunc = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

export function caption(n: GNode): string {
  switch (n.label) {
    case "User": return String(n.display_name ?? n.id);
    case "Fact": case "Summary": case "Inference": return trunc(String(n.text ?? n.id), 30);
    case "Action": return String(n.type ?? n.id).replace(/_/g, " ");
    case "Source": return String(n.type ?? "source").replace(/_/g, " ");
    case "Memory": return `${n.tier ?? ""} memory`.trim();
    case "Agent": return String(n.role ?? n.id);
    case "Tenant": return String(n.name ?? n.id);
    default: return n.id;
  }
}

export interface Visible { nodes: GNode[]; edges: GEdge[]; placement: Placement }

export function visible(graph: GraphData, showContext: boolean): Visible {
  const placement = layout(graph.nodes, graph.edges);
  const targets = leakTargets(graph.edges);
  const nodes = graph.nodes.filter((n) => showContext || !isContext(n, targets));
  const ids = new Set(nodes.map((n) => n.id));
  const edges = graph.edges.filter((e) => ids.has(e.src) && ids.has(e.dst));
  return { nodes, edges, placement };
}

const nodeEl = (n: GNode, p: { x: number; y: number }): ElementDefinition =>
  ({ group: "nodes", data: { id: n.id, label: n.label, caption: caption(n) }, position: { ...p } });
const edgeEl = (e: GEdge): ElementDefinition => ({ group: "edges", data: { id: e.id, source: e.src, target: e.dst, rel: e.rel } });

interface Pending { timers: number[]; removals: string[] }
const pendingOf = (cy: Core): Pending => (cy.scratch("pending") as Pending | undefined) ?? { timers: [], removals: [] };

/** Finish any animated removals right now, so a new sync starts from a clean state. */
export function flushPending(cy: Core): void {
  const p = pendingOf(cy);
  p.timers.forEach((t) => window.clearTimeout(t));
  cy.batch(() => p.removals.forEach((id) => cy.getElementById(id).remove()));
  cy.scratch("pending", { timers: [], removals: [] });
}

export interface SyncResult { added: string[]; removed: string[] }

export function syncGraph(cy: Core, graph: GraphData, showContext: boolean, opts: { animate: boolean; fit: boolean }): SyncResult {
  flushPending(cy);
  const { nodes, edges, placement } = visible(graph, showContext);
  const want = new Map(nodes.map((n) => [n.id, n]));
  const have = new Set(cy.nodes().filter((n) => !n.hasClass("colhead")).map((n) => n.id()));
  const removed = [...have].filter((id) => !want.has(id));
  const added = nodes.filter((n) => !have.has(n.id)).map((n) => n.id);
  const first = have.size === 0;

  cy.batch(() => {
    cy.nodes(".colhead").remove();
    // edges that no longer exist between surviving nodes
    const edgeIds = new Set(edges.map((e) => e.id));
    cy.edges().forEach((e) => { if (!edgeIds.has(e.id()) && !removed.includes(e.source().id()) && !removed.includes(e.target().id())) e.remove(); });
    for (const n of nodes) if (!have.has(n.id)) cy.add(nodeEl(n, placement.pos[n.id] ?? { x: 0, y: 0 }));
    for (const e of edges) if (cy.getElementById(e.id).empty()) cy.add(edgeEl(e));
    placement.columns.forEach((c, i) => cy.add({ group: "nodes", classes: "colhead", data: { id: `col:${i}`, title: c.title }, position: { x: c.x, y: -ROW_ABOVE }, selectable: false, grabbable: false }));
  });

  // removals: redact, fade, then remove (or remove at once when motion is off)
  if (removed.length) {
    if (opts.animate) {
      const p: Pending = { timers: [], removals: removed };
      removed.forEach((id) => { const n = cy.getElementById(id); n.addClass("redacted"); n.connectedEdges().addClass("gone"); });
      p.timers.push(window.setTimeout(() => removed.forEach((id) => cy.getElementById(id).addClass("gone")), 700));
      p.timers.push(window.setTimeout(() => flushPending(cy), 1000));
      cy.scratch("pending", p);
    } else {
      cy.batch(() => removed.forEach((id) => cy.getElementById(id).remove()));
    }
  }

  // positions of surviving nodes follow the new layout
  for (const n of nodes) {
    const el = cy.getElementById(n.id);
    const to = placement.pos[n.id];
    if (el.empty() || !to) continue;
    const from = el.position();
    if (Math.abs(from.x - to.x) < 0.5 && Math.abs(from.y - to.y) < 0.5) continue;
    if (opts.animate && !first && !added.includes(n.id)) el.animate({ position: to }, { duration: 500, easing: "ease-in-out" });
    else el.position(to);
  }
  if (opts.fit || first) cy.fit(cy.elements().not(".gone"), 48);
  return { added, removed };
}
const ROW_ABOVE = 70;

export function applyMarks(cy: Core, marks: Marks, selectedId: string | null, focus: boolean): void {
  cy.batch(() => {
    cy.elements().removeClass(MARK_CLASSES.join(" "));
    const add = (set: Set<string>, cls: string) => set.forEach((id) => { const el = cy.getElementById(id); if (!el.empty() && !el.hasClass("redacted")) el.addClass(cls); });
    add(marks.erase, "erase"); add(marks.shared, "shared"); add(marks.keep, "keep"); add(marks.leak, "leak");
    add(marks.fresh, "fresh"); add(marks.up, "up"); add(marks.down, "down");
    if (focus) {
      const lit = new Set<string>([...marks.erase, ...marks.shared, ...marks.keep, ...marks.leak, ...marks.up, ...marks.down]);
      if (selectedId) lit.add(selectedId);
      cy.nodes().not(".colhead").forEach((n) => { if (!lit.has(n.id())) n.addClass("dim"); });
      cy.edges().forEach((e) => { if (e.source().hasClass("dim") || e.target().hasClass("dim")) e.addClass("dim"); });
    }
    cy.nodes(":selected").unselect();
    if (selectedId) { const el = cy.getElementById(selectedId); if (!el.empty()) el.select(); }
  });
}
