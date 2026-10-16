import cytoscape, { type Core } from "cytoscape";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import acme from "./fixtures/acme.json";
import { applyMarks, caption, emptyMarks, flushPending, syncGraph, visible } from "./graphCore";
import type { GraphData } from "./types";

const A = acme as unknown as GraphData;
const without = (g: GraphData, ids: string[]): GraphData => ({
  ...g, nodes: g.nodes.filter((n) => !ids.includes(n.id)), edges: g.edges.filter((e) => !ids.includes(e.src) && !ids.includes(e.dst)),
});
const real = (cy: Core) => cy.nodes().filter((n) => !n.hasClass("colhead"));

describe("graph core (headless Cytoscape)", () => {
  let cy: Core;
  beforeEach(() => { vi.useFakeTimers(); cy = cytoscape({ headless: true, styleEnabled: false }); });
  afterEach(() => { cy.destroy(); vi.useRealTimers(); });

  it("draws the main graph and adds memories and agents only on request", () => {
    syncGraph(cy, A, false, { animate: false, fit: false });
    const main = visible(A, false);
    expect(real(cy).length).toBe(main.nodes.length);
    expect(cy.nodes(".colhead").length).toBeGreaterThan(3);
    expect(cy.getElementById("globex").nonempty()).toBe(true);               // leak destination always shown
    expect(cy.getElementById("mem_concierge_ravi").empty()).toBe(true);
    expect(cy.edges("[rel = 'CONTAINS']").length).toBe(0);
    syncGraph(cy, A, true, { animate: false, fit: false });
    expect(real(cy).length).toBe(A.nodes.length);
    expect(cy.edges("[rel = 'CONTAINS']").length).toBeGreaterThan(0);
  });

  it("keeps edge directions as child to parent", () => {
    syncGraph(cy, A, false, { animate: false, fit: false });
    const e = cy.getElementById("sum_ravi_overview|SUMMARIZES|sum_ravi_profile");
    expect(e.source().id()).toBe("sum_ravi_overview");
    expect(e.target().id()).toBe("sum_ravi_profile");
  });

  it("marks erase, keep and shared nodes and dims everything else when focused", () => {
    syncGraph(cy, A, false, { animate: false, fit: false });
    const m = emptyMarks();
    m.erase = new Set(["fact_ravi_vegetarian", "sum_team_q4"]); m.shared = new Set(["sum_team_q4"]); m.keep = new Set(["src_meeting_team_001"]);
    applyMarks(cy, m, "fact_ravi_vegetarian", true);
    expect(cy.getElementById("fact_ravi_vegetarian").hasClass("erase")).toBe(true);
    expect(cy.getElementById("sum_team_q4").hasClass("shared")).toBe(true);
    expect(cy.getElementById("src_meeting_team_001").hasClass("keep")).toBe(true);
    expect(cy.getElementById("fact_priya_email_pref").hasClass("dim")).toBe(true);
    expect(cy.getElementById("fact_ravi_vegetarian").hasClass("dim")).toBe(false);
    expect(cy.getElementById("fact_ravi_vegetarian").selected()).toBe(true);
    applyMarks(cy, emptyMarks(), null, false);
    expect(cy.elements(".erase, .dim, .shared, .keep").length).toBe(0);
    expect(cy.nodes(":selected").length).toBe(0);
  });

  it("removes erased nodes at once when motion is off", () => {
    syncGraph(cy, A, false, { animate: false, fit: false });
    const gone = ["fact_ravi_vegetarian", "inf_ravi_lisbon_veg"];
    const res = syncGraph(cy, without(A, gone), false, { animate: false, fit: false });
    expect(res.removed.sort()).toEqual(gone.sort());
    expect(cy.getElementById("fact_ravi_vegetarian").empty()).toBe(true);
    expect(cy.edges().filter((e) => gone.includes(e.source().id()) || gone.includes(e.target().id())).length).toBe(0);
  });

  it("redacts, then fades, then removes erased nodes when motion is on", () => {
    syncGraph(cy, A, false, { animate: false, fit: false });
    syncGraph(cy, without(A, ["fact_ravi_vegetarian"]), false, { animate: true, fit: false });
    const n = cy.getElementById("fact_ravi_vegetarian");
    expect(n.hasClass("redacted")).toBe(true);
    vi.advanceTimersByTime(750);
    expect(cy.getElementById("fact_ravi_vegetarian").hasClass("gone")).toBe(true);
    vi.advanceTimersByTime(300);
    expect(cy.getElementById("fact_ravi_vegetarian").empty()).toBe(true);
  });

  it("finishes a pending removal before the next sync", () => {
    syncGraph(cy, A, false, { animate: false, fit: false });
    syncGraph(cy, without(A, ["fact_ravi_vegetarian"]), false, { animate: true, fit: false });
    flushPending(cy);
    expect(cy.getElementById("fact_ravi_vegetarian").empty()).toBe(true);
    const res = syncGraph(cy, A, false, { animate: true, fit: false });
    expect(res.added).toContain("fact_ravi_vegetarian");
  });

  it("reports newly added nodes", () => {
    const smaller = without(A, ["act_priya_send_update"]);
    syncGraph(cy, smaller, false, { animate: false, fit: false });
    expect(syncGraph(cy, A, false, { animate: false, fit: false }).added).toEqual(["act_priya_send_update"]);
  });

  it("writes short, readable captions from node properties", () => {
    const n = (label: string, props: object) => ({ id: "x", label, ...props }) as never;
    expect(caption(n("User", { display_name: "Ravi" }))).toBe("Ravi");
    expect(caption(n("Action", { type: "send_email_update" }))).toBe("send email update");
    expect(caption(n("Fact", { text: "x".repeat(60) })).length).toBe(30);
    expect(caption(n("Memory", { tier: "episodic" }))).toBe("episodic memory");
  });
});
