import { describe, expect, it } from "vitest";
import acme from "./fixtures/acme.json";
import globex from "./fixtures/globex.json";
import { COL_GAP, ROW_GAP, isContext, layout, leakTargets } from "./layout";
import type { GraphData } from "./types";

// Real exports of the seeded tenant graphs (generated from FalkorDB through the API's export function).
const A = acme as unknown as GraphData;
const G = globex as unknown as GraphData;
const byId = (g: GraphData) => new Map(g.nodes.map((n) => [n.id, n]));

describe("layered layout", () => {
  const p = layout(A.nodes, A.edges);

  it("places every node", () => {
    expect(Object.keys(p.pos).sort()).toEqual(A.nodes.map((n) => n.id).sort());
  });

  it("keeps nodes in one column from overlapping", () => {
    const cols = new Map<number, number[]>();
    for (const n of A.nodes) {
      if (isContext(n, leakTargets(A.edges))) continue;
      const { x, y } = p.pos[n.id];
      cols.set(x, [...(cols.get(x) ?? []), y]);
    }
    for (const ys of cols.values()) {
      const s = [...ys].sort((a, b) => a - b);
      for (let i = 1; i < s.length; i++) expect(s[i] - s[i - 1]).toBeGreaterThanOrEqual(ROW_GAP * 0.89);
    }
  });

  it("flows from origin on the left to derivatives on the right", () => {
    const nodes = byId(A);
    for (const e of A.edges) {
      if (!["SUMMARIZES", "INFERRED_FROM", "BASED_ON"].includes(e.rel)) continue;
      expect(p.pos[e.src].x, `${e.src} -> ${e.dst}`).toBeGreaterThan(p.pos[e.dst].x);
    }
    const x = (label: string) => Math.min(...A.nodes.filter((n) => n.label === label).map((n) => p.pos[n.id].x));
    expect(x("User")).toBeLessThan(x("Source"));
    expect(x("Source")).toBeLessThan(x("Fact"));
    expect(x("Fact")).toBeLessThan(x("Summary"));
    expect(Math.max(...A.nodes.filter((n) => n.label === "Inference" || n.label === "Summary").map((n) => p.pos[n.id].x)))
      .toBeLessThan(Math.min(...A.nodes.filter((n) => n.label === "Action").map((n) => p.pos[n.id].x)));
    expect(nodes.size).toBe(A.nodes.length);
  });

  it("puts a summary of a summary one column deeper than its parent", () => {
    expect(p.pos["sum_ravi_overview"].x - p.pos["sum_ravi_profile"].x).toBe(COL_GAP);
  });

  it("keeps each person's facts together and gives a mixed node a height between theirs", () => {
    const y = (id: string) => p.pos[id].y;
    const mixed = y("sum_team_q4");
    expect(mixed).toBeGreaterThan(Math.min(y("fact_ravi_owns_onboarding"), y("fact_priya_owns_budget")) - 1);
    expect(mixed).toBeLessThan(Math.max(y("fact_ravi_owns_onboarding"), y("fact_priya_owns_budget")) + 1);
    expect(Math.abs(y("fact_ravi_vegetarian") - y("fact_ravi_trip_lisbon"))).toBeLessThan(Math.abs(y("fact_ravi_vegetarian") - y("fact_priya_email_pref")));
  });

  it("shows the leak destination in its own rightmost column and the context lane underneath", () => {
    expect(leakTargets(A.edges)).toEqual(new Set(["globex"]));
    expect(p.pos["globex"].x).toBe(Math.max(...Object.values(p.pos).filter((_, i) => i >= 0).map((q) => q.x)));
    expect(p.columns.map((c) => c.title)).toContain("Other tenants");
    const mainBottom = Math.max(...A.nodes.filter((n) => !isContext(n, leakTargets(A.edges))).map((n) => p.pos[n.id].y));
    for (const n of A.nodes.filter((n) => n.label === "Memory" || n.label === "Agent")) expect(p.pos[n.id].y).toBeGreaterThan(mainBottom);
  });

  it("is deterministic", () => {
    expect(layout(A.nodes, A.edges)).toEqual(p);
  });

  it("handles the small Globex graph and an empty graph", () => {
    const g = layout(G.nodes, G.edges);
    expect(Object.keys(g.pos).sort()).toEqual(G.nodes.map((n) => n.id).sort());
    expect(layout([], [])).toEqual({ pos: {}, columns: [], width: 0, height: 0 });
  });

  it("survives a derivation cycle without hanging", () => {
    const nodes = [{ id: "a", label: "Summary" }, { id: "b", label: "Summary" }] as GraphData["nodes"];
    const edges = [{ id: "1", src: "a", rel: "SUMMARIZES", dst: "b" }, { id: "2", src: "b", rel: "SUMMARIZES", dst: "a" }];
    expect(Object.keys(layout(nodes, edges).pos)).toHaveLength(2);
  });
});
