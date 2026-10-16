import type { QueryEntry } from "./types";

export function QueryPanel({ entries }: { entries: QueryEntry[] }) {
  return (
    <div className="panel-body">
      <p className="lede">The Cypher that actually ran for each action, newest first.</p>
      {entries.length === 0 && <p className="small">Nothing yet. Preview an erasure or inspect a node.</p>}
      {entries.map((e, i) => (
        <details key={e.at + i} open={i === 0}>
          <summary>{e.source} <span className="small">{e.at}, {e.cypher.length} {e.cypher.length === 1 ? "statement" : "statements"}</span></summary>
          {e.cypher.map((q, j) => (
            <div key={j}>
              <pre>{q}</pre>
              {e.params[j] && Object.keys(e.params[j]).length > 0 && <pre className="params">{JSON.stringify(e.params[j], null, 1)}</pre>}
            </div>
          ))}
        </details>
      ))}
    </div>
  );
}
