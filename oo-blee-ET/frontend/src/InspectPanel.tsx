import { useState } from "react";
import type { GraphData, GNode, ProvPath } from "./types";

export interface Detail {
  supported?: boolean;
  explanation?: string;
  agents?: string[];
  upstream?: ProvPath[];
  downstream?: ProvPath[];
  truncated?: boolean;
}

const VERB: Record<string, string> = { BASED_ON: "is based on", INFERRED_FROM: "is inferred from", SUMMARIZES: "summarizes", DERIVED_FROM: "came from", ABOUT: "is about" };
const SHOW = ["id", "label", "text", "tier", "type", "status", "confidence", "sensitivity", "agent_id", "tenant_id", "created_at", "display_name", "role", "name", "session_id", "url_or_session"];

function Chain({ path, onSelect }: { path: ProvPath; onSelect: (id: string) => void }) {
  return (
    <li className="chain">
      {path.nodes.map((n, i) => (
        <span key={n.id + i}>
          {i > 0 && <span className="rel"> {VERB[path.edges[i - 1]?.rel] ?? path.edges[i - 1]?.rel} </span>}
          <button type="button" className="link" onClick={() => onSelect(n.id)}>{n.id}</button>
        </span>
      ))}
    </li>
  );
}

interface Props {
  graph: GraphData | null;
  selectedId: string | null;
  node: GNode | null;
  detail: Detail | null;
  loading: boolean;
  error: string | null;
  onSelect: (id: string | null) => void;
}

export function InspectPanel({ selectedId, node, detail, loading, error, onSelect }: Props) {
  const [typed, setTyped] = useState("");
  return (
    <div className="panel-body">
      <p className="lede">Click a node in the graph, or enter an id. Its origins and dependents light up.</p>
      <form className="fields inline" onSubmit={(e) => { e.preventDefault(); if (typed.trim()) onSelect(typed.trim()); }}>
        <label>Node id<input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="sum_team_q4" /></label>
        <button type="submit">Inspect</button>
      </form>
      {!selectedId && <p className="small">Nothing selected.</p>}
      {error && <p className="alert" role="alert">{error}</p>}
      {node && (
        <>
          <h3>{node.label}</h3>
          <dl className="props">
            {SHOW.filter((k) => node[k] !== undefined).map((k) => <div key={k}><dt>{k}</dt><dd>{String(node[k])}</dd></div>)}
          </dl>
        </>
      )}
      {selectedId && loading && <p className="small">Reading the graph…</p>}
      {detail?.explanation && (
        <>
          <h3>Why it exists</h3>
          <p className={detail.supported ? "" : "note"}>{detail.explanation}</p>
          {detail.agents && detail.agents.length > 0 && <p className="small">Contributing agents: {detail.agents.join(", ")}.</p>}
        </>
      )}
      {detail?.upstream && detail.upstream.length > 0 && (
        <>
          <h3>Where it came from</h3>
          <ul className="chains">{detail.upstream.map((p, i) => <Chain key={i} path={p} onSelect={onSelect} />)}</ul>
        </>
      )}
      {detail?.downstream && detail.downstream.length > 0 && (
        <>
          <h3>What depends on it</h3>
          <ul className="chains">{detail.downstream.map((p, i) => <Chain key={i} path={{ nodes: [...p.nodes].reverse(), edges: [...p.edges].reverse() }} onSelect={onSelect} />)}</ul>
        </>
      )}
      {detail?.truncated && <p className="note">The walk stopped at its depth limit, so this list may be incomplete.</p>}
    </div>
  );
}
