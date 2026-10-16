import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import acme from "./fixtures/acme.json";
import { ErasePanel } from "./ErasePanel";
import { InspectPanel } from "./InspectPanel";
import type { GraphData, Preview } from "./types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const A = acme as unknown as GraphData;
const users = [{ id: "user_priya", display_name: "Priya" }, { id: "user_ravi", display_name: "Ravi" }];
const preview = (over: Partial<Preview> = {}): Preview => ({
  user_id: "user_ravi", tenant_id: "acme", preview_id: "prev_abc", preview_version: "v".repeat(64), candidates: [], node_ids: ["a", "b", "c"],
  edges: [{ src: "a", rel: "ABOUT", dst: "b" }], counts_by_node_type: { Fact: 2, User: 1 }, counts_by_edge_type: { ABOUT: 1 },
  affected_agents: ["agent_concierge"], affected_memories: { deleted: [], kept_with_other_content: ["mem_x"] }, derived: { summaries: [], inferences: [], actions: [] },
  kept_sources: ["src_shared"], shared_derived_nodes: [{ node_id: "sum_team_q4", other_user_ids: ["user_priya"] }],
  cross_tenant_findings: [{ source_node_id: "fact_x", destination_tenant: "globex", status: "x" }], warnings: [], blocking: [], can_execute: true, policy: {}, ...over,
});

let host: HTMLDivElement, root: Root;
beforeEach(() => { host = document.createElement("div"); document.body.append(host); root = createRoot(host); });
afterEach(() => { act(() => root.unmount()); host.remove(); });

function erase(over: Record<string, unknown> = {}) {
  const props = {
    tenant: "acme", users, userId: "user_ravi", onUser: vi.fn(), options: [{ key: "self", label: "Ravi, the data subject", acting: { principal_id: "user_ravi", role: "data_subject" as const } }],
    actingKey: "self", onActing: vi.fn(), graph: A, preview: null, receipt: null, audit: null, leaks: null, busy: null, error: null,
    onPreview: vi.fn(), onExecute: vi.fn(), onShowTenant: vi.fn(), ...over,
  };
  act(() => root.render(<ErasePanel {...props} />));
  return props;
}
const btn = (text: RegExp) => [...host.querySelectorAll("button")].find((b) => text.test(b.textContent ?? "")) as HTMLButtonElement;
const type = (input: HTMLInputElement, value: string) => act(() => {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
});

describe("erase panel", () => {
  it("cannot erase before a preview", () => {
    erase();
    expect(btn(/Erase Ravi/).disabled).toBe(true);
    expect(host.textContent).toContain("Run a preview first");
    btn(/Preview erasing Ravi/).click();
  });

  it("shows counts from the preview and what stays, and gates erasing on typing the user id", () => {
    const p = erase({ preview: preview() });
    expect(host.textContent).toContain("3 nodes and 1 edge would be removed");
    expect(host.textContent).toContain(`${A.nodes.length - 3} nodes in this graph stay untouched`);
    expect(host.textContent).toMatch(/another person's data \(sum_team_q4\)/);
    expect(host.textContent).toMatch(/Kept because others still use them: src_shared, mem_x/);
    expect(host.textContent).toMatch(/fact_x is marked as shared with globex/);
    expect(btn(/Erase Ravi/).disabled).toBe(true);
    const confirm = host.querySelectorAll("input")[1] as HTMLInputElement;
    type(confirm, "user_pri");
    expect(btn(/Erase Ravi/).disabled).toBe(true);
    type(confirm, "user_ravi");
    expect(btn(/Erase Ravi/).disabled).toBe(false);
    btn(/Erase Ravi/).click();
    expect(p.onExecute).toHaveBeenCalledWith("Erasure requested by the data subject");
  });

  it("blocks erasing and explains why when the preview cannot be executed", () => {
    erase({ preview: preview({ can_execute: false, blocking: [{ code: "TRUNCATED_TRAVERSAL", severity: "blocking", message: "walk stopped at 8 hops" }] }) });
    expect(host.querySelector(".alert")?.textContent).toContain("Erasure is blocked");
    const confirm = host.querySelectorAll("input")[1] as HTMLInputElement;
    type(confirm, "user_ravi");
    expect(btn(/Erase Ravi/).disabled).toBe(true);
  });

  it("shows the receipt, both audits and the limits of a graph audit, and offers the other tenant", () => {
    const audit = { graph_audit_passed: true, covered_paths_remaining: 0, covered_nodes_remaining: [], user_node_present: false, deleted_ids_still_present: [],
      unresolved_warnings: [], scope: { tenant_graphs_checked: ["tenant_acme"], external_systems_checked: false }, limitations: "A graph audit does not prove deletion from logs." };
    const receipt = { job_id: "erase_1", user_id: "user_ravi", tenant_id: "acme", requested_at: "t0", completed_at: "t1", reason: "r", deleted_node_ids: ["a", "b"],
      deleted_edge_descriptors: [{ src: "a", rel: "ABOUT", dst: "b" }], counts_by_node_type: {}, counts_by_edge_type: {}, affected_agents: [], cross_tenant_findings: [],
      preview_version: "v", audit_result: audit, warnings: [], integrity: "none: this receipt is not signed or tamper-proof" };
    const leaks = { findings: [{ source_fact_id: "f", source_tenant: "acme", destination_tenant: "globex", subject_user_id: null, declared_via_leaked_into: false, status: "undeclared_copy", copies: [] }],
      remediation_preview: { requires_explicit_authorization: true, executed: false, tenant: "globex", proposed_node_ids: [], note: "Nothing in the other tenant was changed." } };
    const p = erase({ receipt, audit, leaks });
    expect(host.textContent).toContain("Removed 2 nodes and 1 edge");
    expect(host.textContent).toContain("Audit run with the erasure: passed, 0 covered paths remain");
    expect(host.textContent).toContain("Second audit by the Auditor");
    expect(host.textContent).toContain("does not prove deletion from logs");
    expect(host.textContent).toContain("not signed or tamper-proof");
    expect(host.textContent).toMatch(/A copy remains in globex, but acme no longer records/);
    expect(host.textContent).toContain("Nothing in the other tenant was changed");
    btn(/Show it in globex/).click();
    expect(p.onShowTenant).toHaveBeenCalledWith("globex");
  });

  it("disables everything while a request is running", () => {
    erase({ preview: preview(), busy: "execute" });
    expect(btn(/Erasing/).disabled).toBe(true);
    expect(host.querySelector("select")!.disabled).toBe(true);
  });
});

describe("inspect panel", () => {
  it("lists provenance chains whose ids select the node", () => {
    const onSelect = vi.fn();
    const detail = { supported: true, explanation: "sum_x is a Summary.", agents: ["agent_analyst"],
      upstream: [{ nodes: [{ id: "sum_x" }, { id: "fact_y" }], edges: [{ src: "sum_x", rel: "SUMMARIZES", dst: "fact_y" }] }], downstream: [] };
    act(() => root.render(<InspectPanel graph={A} selectedId="sum_x" node={{ id: "sum_x", label: "Summary", text: "hello" }} detail={detail} loading={false} error={null} onSelect={onSelect} />));
    expect(host.textContent).toContain("sum_x summarizes fact_y");
    expect(host.textContent).toContain("Contributing agents: agent_analyst");
    btn(/^fact_y$/).click();
    expect(onSelect).toHaveBeenCalledWith("fact_y");
  });

  it("flags an unsupported node and a truncated walk", () => {
    act(() => root.render(<InspectPanel graph={A} selectedId="s" node={null} detail={{ supported: false, explanation: "Evidence unavailable: x", truncated: true }} loading={false} error={null} onSelect={vi.fn()} />));
    expect(host.querySelector(".note")?.textContent).toContain("Evidence unavailable");
    expect(host.textContent).toContain("depth limit");
  });
});
