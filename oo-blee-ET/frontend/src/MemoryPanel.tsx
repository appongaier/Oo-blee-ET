import { useState } from "react";

interface Props {
  users: { id: string; display_name: string }[];
  userId: string;
  onUser: (id: string) => void;
  busy: boolean;
  error: string | null;
  last: { fact_id: string; user: string } | null;
  onAdd: (text: string) => Promise<boolean>;
}

export function MemoryPanel({ users, userId, onUser, busy, error, last, onAdd }: Props) {
  const [text, setText] = useState("");
  const name = users.find((u) => u.id === userId)?.display_name ?? "the user";
  return (
    <div className="panel-body">
      <p className="lede">Tell the Concierge something. It is stored as an episodic memory with its source, container and author.</p>
      <form className="fields stack" onSubmit={async (e) => { e.preventDefault(); if (await onAdd(text)) setText(""); }}>
        <label>About
          <select value={userId} onChange={(e) => onUser(e.target.value)} disabled={busy}>
            {users.map((u) => <option key={u.id} value={u.id}>{u.display_name}</option>)}
          </select>
        </label>
        <label>What {name} said
          <textarea value={text} onChange={(e) => setText(e.target.value)} maxLength={1000} rows={3} placeholder={`${name} plays chess on weekends.`} />
        </label>
        <button type="submit" className="primary" disabled={busy || !text.trim() || !userId}>{busy ? "Saving…" : "Save memory"}</button>
      </form>
      {error && <p className="alert" role="alert">{error}</p>}
      {last && <p aria-live="polite">Saved as <code>{last.fact_id}</code>. It is highlighted in the graph.</p>}
      <p className="small">This uses the Concierge's MCP tools directly. To have the Analyst and Auditor derive and check new nodes, use the Agents tab.</p>
    </div>
  );
}
