import { useState } from "react";
import type { AgentResponse } from "./types";

type Pipeline = { concierge: AgentResponse; analyst?: AgentResponse; auditor?: AgentResponse; handoffs: { seq: number; from: string; to: string; reason: string; node_ids: string[] }[]; stopped?: string };

interface Props {
  available: boolean;
  busy: boolean;
  error: string | null;
  asked: AgentResponse | null;
  pipeline: Pipeline | null;
  onAsk: (role: string, message: string) => void;
  onPipeline: (statements: string[]) => void;
}

function Reply({ r }: { r: AgentResponse }) {
  return (
    <div className="result">
      <h3>{r.agent_id}</h3>
      <p className="reply">{r.response}</p>
      {r.warnings.length > 0 && <ul className="warns">{r.warnings.map((w, i) => <li key={i}><strong>{w.code}</strong> {w.message}</li>)}</ul>}
      <p className="small">{r.path.length} provenance {r.path.length === 1 ? "path" : "paths"}, {r.cypher.length} {r.cypher.length === 1 ? "query" : "queries"}.</p>
      <ul className="trace">{r.tool_trace.map((t) => <li key={t.seq}>{t.tool}: {t.status}{t.error_code ? ` (${t.error_code})` : ""}</li>)}</ul>
    </div>
  );
}

export function AgentsPanel({ available, busy, error, asked, pipeline, onAsk, onPipeline }: Props) {
  const [role, setRole] = useState("auditor");
  const [message, setMessage] = useState("Why does sum_team_q4 exist?");
  const [lines, setLines] = useState("Ravi plays chess on weekends.\nRavi is learning Portuguese.");
  if (!available) {
    return (
      <div className="panel-body">
        <p className="lede">The agents need a language model.</p>
        <p>Set <code>ANTHROPIC_API_KEY</code> in <code>.env</code>, restart the API, and this tab will let you ask each agent a question or run the full Concierge, Analyst and Auditor pipeline.</p>
        <p className="small">Everything else in this app works without a key.</p>
      </div>
    );
  }
  return (
    <div className="panel-body">
      <p className="lede">Every answer shows the graph paths and queries behind it. Claims without evidence are flagged.</p>
      <form className="fields stack" onSubmit={(e) => { e.preventDefault(); onAsk(role, message); }}>
        <label>Ask
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="concierge">Concierge</option><option value="analyst">Analyst</option><option value="auditor">Auditor</option>
          </select>
        </label>
        <label>Question<textarea rows={2} value={message} onChange={(e) => setMessage(e.target.value)} maxLength={1000} /></label>
        <button type="submit" className="primary" disabled={busy || !message.trim()}>{busy ? "Working…" : "Ask"}</button>
      </form>
      <form className="fields stack" onSubmit={(e) => { e.preventDefault(); onPipeline(lines.split("\n").map((s) => s.trim()).filter(Boolean)); }}>
        <label>New memories, one per line<textarea rows={3} value={lines} onChange={(e) => setLines(e.target.value)} /></label>
        <button type="submit" disabled={busy || !lines.trim()}>Run Concierge, Analyst, Auditor</button>
      </form>
      {error && <p className="alert" role="alert">{error}</p>}
      {asked && <Reply r={asked} />}
      {pipeline && (
        <>
          {pipeline.stopped && <p className="note">{pipeline.stopped}</p>}
          <Reply r={pipeline.concierge} />
          {pipeline.analyst && <Reply r={pipeline.analyst} />}
          {pipeline.auditor && <Reply r={pipeline.auditor} />}
          {pipeline.handoffs.length > 0 && (
            <>
              <h3>Handoffs</h3>
              <ol className="trace">{pipeline.handoffs.map((h) => <li key={h.seq}>{h.from} to {h.to}: {h.reason} ({h.node_ids.length} nodes)</li>)}</ol>
            </>
          )}
        </>
      )}
    </div>
  );
}
