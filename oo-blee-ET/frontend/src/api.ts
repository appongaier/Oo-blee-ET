import type { ActingAs, AgentResponse, AppConfig, AuditResult, GraphData, LeakResult, Preview, ProvPath, Receipt, ToolOutput } from "./types";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details: Record<string, unknown> = {}) {
    super(message);
  }
}

async function request<T>(path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, body === undefined ? undefined : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "NETWORK", "Cannot reach the API. Is the backend running on port 8000?");
  }
  let data: unknown = null;
  try { data = await res.json(); } catch { /* non-JSON body */ }
  if (!res.ok) {
    const e = (data as { error?: { code: string; message: string; details?: Record<string, unknown> } } | null)?.error;
    if (e) throw new ApiError(res.status, e.code, e.message, e.details ?? {});
    const d = (data as { detail?: unknown } | null)?.detail;
    throw new ApiError(res.status, "REQUEST_INVALID", typeof d === "string" ? d : "The request was not valid.");
  }
  return data as T;
}

export const api = {
  config: () => request<AppConfig>("/api/config"),
  graph: (tenant: string) => request<GraphData>(`/api/graph/${tenant}`),
  reset: () => request<{ graphs: Record<string, unknown> }>("/api/demo/reset", {}),
  remember: (tenant: string, user_id: string, text: string) =>
    request<ToolOutput<{ fact_id: string }>>("/api/memory", { tenant, user_id, text }),
  explain: (tenant: string, id: string) =>
    request<ToolOutput<{ paths: ProvPath[]; supported: boolean; explanation: string; agents: string[]; sources: string[]; subjects: string[]; direct_parents: { id: string; rel: string; type: string }[]; memories: { memory_id: string; tier: string }[] }>>(`/api/nodes/${tenant}/${id}/explain`),
  trace: (tenant: string, id: string) =>
    request<ToolOutput<{ upstream: { paths: ProvPath[]; node_ids: string[]; truncated: boolean }; downstream: { paths: ProvPath[]; node_ids: string[]; truncated: boolean }; complete: boolean }>>(`/api/nodes/${tenant}/${id}/trace`),
  preview: (tenant: string, user_id: string, acting_as: ActingAs) =>
    request<ToolOutput<{ preview: Preview; can_execute: boolean; mutated: boolean }>>("/api/erasure/preview", { tenant, user_id, acting_as }),
  execute: (tenant: string, user_id: string, preview_version: string, reason: string, acting_as: ActingAs) =>
    request<ToolOutput<{ receipt: Receipt; receipt_path: string | null }>>("/api/erasure/execute", { tenant, user_id, preview_version, reason, acting_as }),
  audit: (tenant: string, user_id: string, deleted_node_ids: string[], text_terms: string[]) =>
    request<ToolOutput<AuditResult>>("/api/audit", { tenant, user_id, deleted_node_ids, text_terms }),
  leaks: (tenant_a: string, tenant_b: string) => request<ToolOutput<LeakResult>>("/api/leaks", { tenant_a, tenant_b }),
  ask: (tenant: string, user_id: string, role: string, message: string) =>
    request<{ response: AgentResponse; handoffs: unknown[] }>("/api/agents/ask", { tenant, user_id, role, message }),
  pipeline: (tenant: string, user_id: string, statements: string[]) =>
    request<{ concierge: AgentResponse; analyst?: AgentResponse; auditor?: AgentResponse; handoffs: { seq: number; from: string; to: string; reason: string; node_ids: string[] }[]; stopped?: string }>("/api/agents/pipeline", { tenant, user_id, statements }),
};

/** Short, plain wording for the errors a person can actually fix. */
export function explainError(e: unknown): string {
  if (!(e instanceof ApiError)) return "Something went wrong.";
  const m: Record<string, string> = {
    FORBIDDEN_ROLE: "This role cannot do that. Auditors can preview an erasure but not perform one.",
    FORBIDDEN_SUBJECT: "A data subject can only act on their own data. Choose a different identity to act as.",
    FORBIDDEN_TENANT: "That identity has no authority over this tenant.",
    UNAUTHORIZED: "An identity is required for this action.",
    PREVIEW_STALE: "The data changed after the preview. Review the new preview before erasing.",
    ERASURE_BLOCKED: "Erasure is blocked because the provenance could not be fully checked. Nothing was deleted.",
    USER_NOT_FOUND: "That user no longer exists in this tenant.",
    NOT_SEEDED: "This tenant has no graph yet. Reset the demo data.",
    LLM_NOT_CONFIGURED: "The agents need an Anthropic API key. Set ANTHROPIC_API_KEY in .env and restart the API.",
    NETWORK: e.message,
  };
  return m[e.code] ?? e.message;
}
