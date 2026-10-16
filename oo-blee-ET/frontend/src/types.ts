// Shapes of the API responses. Everything shown in the UI comes from these: nothing is hard-coded.
export type Label = "Tenant" | "User" | "Agent" | "Source" | "Fact" | "Summary" | "Inference" | "Action" | "Memory";

export interface GNode { id: string; label: Label; [prop: string]: unknown }
export interface GEdge { id: string; src: string; rel: string; dst: string }
export interface GraphData {
  tenant: string;
  graph: string;
  nodes: GNode[];
  edges: GEdge[];
  counts: { nodes: Record<string, number>; edges: Record<string, number> };
  version: string;
}

export interface TenantInfo { id: string; graph: string; seeded: boolean; users: { id: string; display_name: string }[] }
export interface AppConfig { tenants: TenantInfo[]; llm_available: boolean; demo_controls: boolean; disclaimer: string }

export interface Warning { code: string; severity: string; message: string; node_id?: string }
export interface ToolOutput<R = Record<string, unknown>> {
  result: R;
  cypher: string[];
  params: Record<string, unknown>[];
  graph: { nodes: GNode[]; edges: { src: string; rel: string; dst: string }[] };
  warnings: Warning[];
}

export interface PathNode { id: string; type?: string | null }
export interface PathEdge { src: string; rel: string; dst: string }
export interface ProvPath { nodes: PathNode[]; edges: PathEdge[] }

export interface Preview {
  user_id: string;
  tenant_id: string;
  preview_id: string;
  preview_version: string;
  candidates: { id: string; type: string; category: string }[];
  node_ids: string[];
  edges: PathEdge[];
  counts_by_node_type: Record<string, number>;
  counts_by_edge_type: Record<string, number>;
  affected_agents: string[];
  affected_memories: { deleted: string[]; kept_with_other_content: string[] };
  derived: { summaries: string[]; inferences: string[]; actions: string[] };
  kept_sources: string[];
  shared_derived_nodes: { node_id: string; other_user_ids: string[] }[];
  cross_tenant_findings: { source_node_id: string; destination_tenant: string; status: string }[];
  warnings: Warning[];
  blocking: Warning[];
  can_execute: boolean;
  policy: Record<string, string>;
}

export interface AuditResult {
  graph_audit_passed: boolean;
  covered_paths_remaining: number;
  covered_nodes_remaining: string[];
  user_node_present: boolean;
  deleted_ids_still_present: string[];
  unresolved_warnings: Warning[];
  scope: { tenant_graphs_checked: string[]; external_systems_checked: boolean };
  limitations: string;
}

export interface Receipt {
  job_id: string;
  user_id: string;
  tenant_id: string;
  requested_at: string;
  completed_at: string;
  reason: string;
  deleted_node_ids: string[];
  deleted_edge_descriptors: PathEdge[];
  counts_by_node_type: Record<string, number>;
  counts_by_edge_type: Record<string, number>;
  affected_agents: string[];
  cross_tenant_findings: { source_node_id: string; destination_tenant: string; status: string }[];
  preview_version: string;
  audit_result: AuditResult;
  warnings: Warning[];
  integrity: string;
}

export interface LeakFinding {
  source_fact_id: string;
  source_tenant: string;
  destination_tenant: string;
  subject_user_id: string | null;
  declared_via_leaked_into: boolean;
  status: string;
  copies: { source_id: string; copy_nodes: { node_id: string; derived_node_ids: string[] }[] }[];
}
export interface LeakResult {
  findings: LeakFinding[];
  remediation_preview: { requires_explicit_authorization: boolean; executed: boolean; tenant: string; proposed_node_ids: string[]; note: string };
}

export interface AgentResponse {
  agent_id: string;
  response: string;
  path: (ProvPath & { source_tool: string })[];
  cypher: string[];
  tool_trace: { seq: number; tool: string; status: string; error_code: string | null; reason: string | null; ms: number }[];
  warnings: Warning[];
}

export interface ActingAs { principal_id: string; role: "data_subject" | "tenant_admin" | "auditor" }

export interface QueryEntry { at: string; source: string; cypher: string[]; params: Record<string, unknown>[] }
