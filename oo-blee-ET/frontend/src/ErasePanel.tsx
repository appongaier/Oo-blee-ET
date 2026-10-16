import { useState } from "react";
import type { ActingAs, AuditResult, GraphData, LeakResult, Preview, Receipt } from "./types";

export interface ActingOption { key: string; label: string; acting: ActingAs }

interface Props {
  tenant: string;
  users: { id: string; display_name: string }[];
  userId: string;
  onUser: (id: string) => void;
  options: ActingOption[];
  actingKey: string;
  onActing: (key: string) => void;
  graph: GraphData | null;
  preview: Preview | null;
  receipt: Receipt | null;
  audit: AuditResult | null;
  leaks: LeakResult | null;
  busy: string | null;
  error: string | null;
  onPreview: () => void;
  onExecute: (reason: string) => void;
  onShowTenant: (tenant: string) => void;
}

const plural = (n: number, one: string, many = one + "s") => `${n} ${n === 1 ? one : many}`;

function leakSentence(status: string, from: string, to: string): string {
  if (status === "copy_found") return `A copy of this data is still in ${to}.`;
  if (status === "undeclared_copy") return `A copy remains in ${to}, but ${from} no longer records that it was shared.`;
  return `${from} marks this data as shared with ${to}, but no copy was found there.`;
}

export function ErasePanel(p: Props) {
  const user = p.users.find((u) => u.id === p.userId);
  const name = user?.display_name ?? p.userId;
  const [reason, setReason] = useState("Erasure requested by the data subject");
  const [confirm, setConfirm] = useState("");
  const busy = p.busy !== null;
  const pv = p.preview;
  const staying = pv && p.graph ? p.graph.nodes.length - pv.node_ids.length : null;
  const canErase = !!pv && pv.can_execute && confirm === p.userId && reason.trim().length > 0 && !busy && !p.receipt;

  const download = () => {
    if (!p.receipt) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(p.receipt, null, 2)], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url; a.download = `${p.receipt.job_id}.json`; a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="panel-body">
      <p className="lede">Preview what would be removed, then authorize it. Nothing is deleted until you confirm.</p>

      <div className="fields">
        <label>Person
          <select value={p.userId} onChange={(e) => { p.onUser(e.target.value); setConfirm(""); }} disabled={busy || !p.users.length}>
            {p.users.map((u) => <option key={u.id} value={u.id}>{u.display_name}</option>)}
          </select>
        </label>
        <label>Acting as
          <select value={p.actingKey} onChange={(e) => p.onActing(e.target.value)} disabled={busy}>
            {p.options.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
          </select>
        </label>
      </div>
      {p.error && <p className="alert" role="alert">{p.error}</p>}

      <ol className="steps">
        <li>
          <h3>Preview</h3>
          <button type="button" className="primary" onClick={p.onPreview} disabled={busy || !p.userId}>
            {p.busy === "preview" ? "Checking the graph…" : pv ? `Preview again` : `Preview erasing ${name}`}
          </button>
          {pv && (
            <div className="result" aria-live="polite">
              <p className="big"><span className="num">{pv.node_ids.length}</span> {pv.node_ids.length === 1 ? "node" : "nodes"} and <span className="num">{pv.edges.length}</span> {pv.edges.length === 1 ? "edge" : "edges"} would be removed.</p>
              <dl className="tally">
                {Object.entries(pv.counts_by_node_type).map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
              </dl>
              {staying !== null && <p>{plural(staying, "node")} in this graph stay untouched, including everyone else's data.</p>}
              {pv.shared_derived_nodes.length > 0 && (
                <p className="note">{plural(pv.shared_derived_nodes.length, "node")} also depend{pv.shared_derived_nodes.length === 1 ? "s" : ""} on another person's data ({pv.shared_derived_nodes.map((s) => s.node_id).join(", ")}). One covered parent is enough, so {pv.shared_derived_nodes.length === 1 ? "it is" : "they are"} removed with the rest.</p>
              )}
              {(pv.kept_sources.length > 0 || pv.affected_memories.kept_with_other_content.length > 0) && (
                <p>Kept because others still use {pv.kept_sources.length + pv.affected_memories.kept_with_other_content.length === 1 ? "it" : "them"}: {[...pv.kept_sources, ...pv.affected_memories.kept_with_other_content].join(", ")}.</p>
              )}
              {pv.cross_tenant_findings.map((f) => (
                <p className="note" key={f.source_node_id}>{f.source_node_id} is marked as shared with {f.destination_tenant}. That copy is not deleted here.</p>
              ))}
              {pv.blocking.length > 0 && (
                <div className="alert" role="alert">
                  <strong>Erasure is blocked.</strong>
                  <ul>{pv.blocking.map((b, i) => <li key={i}>{b.message}{b.node_id ? ` (${b.node_id})` : ""}</li>)}</ul>
                </div>
              )}
              <p className="small">Preview {pv.preview_id}. Agents affected: {pv.affected_agents.join(", ") || "none"}.</p>
            </div>
          )}
        </li>

        <li>
          <h3>Authorize</h3>
          <div className="fields stack">
            <label>Reason
              <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={200} disabled={!pv || busy || !!p.receipt} />
            </label>
            <label>Type <code>{p.userId}</code> to confirm
              <input value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="off" disabled={!pv || busy || !!p.receipt} />
            </label>
          </div>
          <button type="button" className="danger" disabled={!canErase} onClick={() => p.onExecute(reason.trim())}>
            {p.busy === "execute" ? "Erasing…" : `Erase ${name}'s data`}
          </button>
          {!pv && <p className="small">Run a preview first.</p>}
        </li>

        <li>
          <h3>Receipt and audit</h3>
          {!p.receipt && <p className="small">Appears after an erasure.</p>}
          {p.receipt && (
            <div className="result" aria-live="polite">
              <p className="big">Removed <span className="num">{p.receipt.deleted_node_ids.length}</span> nodes and <span className="num">{p.receipt.deleted_edge_descriptors.length}</span> edges.</p>
              <p className="small">Job {p.receipt.job_id}, completed {p.receipt.completed_at}. {p.receipt.integrity}.</p>
              <AuditLine title="Audit run with the erasure" a={p.receipt.audit_result} />
              {p.audit && <AuditLine title="Second audit by the Auditor" a={p.audit} />}
              <p className="small">{p.receipt.audit_result.limitations}</p>
              <button type="button" onClick={download}>Download receipt (JSON)</button>
            </div>
          )}
        </li>

        <li>
          <h3>Other tenants</h3>
          {!p.leaks && <p className="small">Checked after an erasure.</p>}
          {p.leaks && p.leaks.findings.length === 0 && <p>No data from this tenant was found in another tenant.</p>}
          {p.leaks && p.leaks.findings.map((f) => (
            <div className="result" key={f.source_fact_id}>
              <p>{leakSentence(f.status, f.source_tenant, f.destination_tenant)}</p>
              <button type="button" onClick={() => p.onShowTenant(f.destination_tenant)}>Show it in {f.destination_tenant}</button>
            </div>
          ))}
          {p.leaks && p.leaks.findings.length > 0 && <p className="small">{p.leaks.remediation_preview.note}</p>}
        </li>
      </ol>
    </div>
  );
}

function AuditLine({ title, a }: { title: string; a: AuditResult }) {
  return (
    <p>
      <strong>{title}:</strong> {a.graph_audit_passed ? `passed, ${a.covered_paths_remaining} covered paths remain.` : `not passed, ${a.covered_nodes_remaining.length} covered nodes remain.`}
      {a.unresolved_warnings.length > 0 && ` ${plural(a.unresolved_warnings.length, "warning")} to review (${[...new Set(a.unresolved_warnings.map((w) => w.code))].join(", ")}).`}
    </p>
  );
}
