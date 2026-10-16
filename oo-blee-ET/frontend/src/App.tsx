import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgentsPanel } from "./AgentsPanel";
import { api, ApiError, explainError } from "./api";
import { ErasePanel, type ActingOption } from "./ErasePanel";
import { emptyMarks } from "./graphCore";
import { GraphView } from "./GraphView";
import { InspectPanel, type Detail } from "./InspectPanel";
import { MemoryPanel } from "./MemoryPanel";
import { QueryPanel } from "./QueryPanel";
import { dark as darkPalette, light as lightPalette } from "./theme";
import type { AgentResponse, AppConfig, AuditResult, GraphData, LeakResult, Preview, QueryEntry, Receipt, ToolOutput } from "./types";
import { useDark } from "./useDark";

const FALLBACK_DISCLAIMER = "Synthetic demo data. This prototype demonstrates graph-based erasure workflows and is not legal advice or proof of complete regulatory compliance.";
type Tab = "erase" | "inspect" | "memory" | "agents" | "queries";
const TABS: { id: Tab; label: string }[] = [
  { id: "erase", label: "Erase" }, { id: "inspect", label: "Inspect" }, { id: "memory", label: "Memory" },
  { id: "agents", label: "Agents" }, { id: "queries", label: "Queries" },
];
const BELIEFS = new Set(["Fact", "Summary", "Inference", "Action"]);
type Pipeline = Awaited<ReturnType<typeof api.pipeline>>;

export default function App() {
  const dark = useDark();
  const palette = dark ? darkPalette : lightPalette;
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [workTenant, setWorkTenant] = useState("acme");
  const [viewTenant, setViewTenant] = useState("acme");
  const [graph, setGraph] = useState<GraphData | null>(null);
  const [graphError, setGraphError] = useState<string | null>(null);
  const [userId, setUserId] = useState("");
  const [actingKey, setActingKey] = useState("self");
  const [tab, setTab] = useState<Tab>("erase");
  const [showContext, setShowContext] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [upDown, setUpDown] = useState<{ up: string[]; down: string[] }>({ up: [], down: [] });
  const [detailBusy, setDetailBusy] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [audit, setAudit] = useState<AuditResult | null>(null);
  const [leaks, setLeaks] = useState<LeakResult | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [eraseError, setEraseError] = useState<string | null>(null);
  const [memError, setMemError] = useState<string | null>(null);
  const [lastMemory, setLastMemory] = useState<{ fact_id: string; user: string } | null>(null);
  const [agentError, setAgentError] = useState<string | null>(null);
  const [asked, setAsked] = useState<AgentResponse | null>(null);
  const [pipeline, setPipeline] = useState<Pipeline | null>(null);
  const [queries, setQueries] = useState<QueryEntry[]>([]);
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  const [confirmReset, setConfirmReset] = useState(false);
  const detailSeq = useRef(0);

  const logQ = useCallback((source: string, out: ToolOutput<unknown>) => {
    setQueries((q) => [{ at: new Date().toLocaleTimeString(), source, cypher: out.cypher, params: out.params }, ...q].slice(0, 60));
  }, []);

  // ---------- loading ----------
  const loadConfig = useCallback(async () => {
    try { const c = await api.config(); setConfig(c); setFatal(null); return c; }
    catch (e) { setFatal(explainError(e)); return null; }
  }, []);
  const loadGraph = useCallback(async (tenant: string) => {
    try { setGraph(await api.graph(tenant)); setGraphError(null); }
    catch (e) { setGraph(null); setGraphError(explainError(e)); }
  }, []);

  useEffect(() => { void loadConfig(); }, [loadConfig]);
  useEffect(() => { setGraph(null); setSelected(null); void loadGraph(viewTenant); }, [viewTenant, loadGraph]);

  const users = useMemo(() => config?.tenants.find((t) => t.id === workTenant)?.users ?? [], [config, workTenant]);
  useEffect(() => {
    if (!users.length) { setUserId(""); return; }
    if (!users.some((u) => u.id === userId)) setUserId((users.find((u) => u.id === "user_ravi") ?? users[0]).id);
  }, [users, userId]);

  const userName = users.find((u) => u.id === userId)?.display_name ?? userId;
  const options: ActingOption[] = useMemo(() => [
    { key: "self", label: `${userName}, the data subject`, acting: { principal_id: userId, role: "data_subject" } },
    ...users.filter((u) => u.id !== userId).map((u) => ({ key: u.id, label: `${u.display_name}, another data subject`, acting: { principal_id: u.id, role: "data_subject" as const } })),
    { key: "admin", label: "Tenant admin", acting: { principal_id: "admin_1", role: "tenant_admin" as const } },
    { key: "auditor", label: "Auditor, preview only", acting: { principal_id: "agent_auditor", role: "auditor" as const } },
  ], [users, userId, userName]);
  const acting = (options.find((o) => o.key === actingKey) ?? options[0]).acting;

  const clearRun = () => { setPreview(null); setReceipt(null); setAudit(null); setLeaks(null); setEraseError(null); };
  const chooseUser = (id: string) => { setUserId(id); setActingKey("self"); clearRun(); };
  const chooseTenant = (t: string) => { setWorkTenant(t); setViewTenant(t); clearRun(); setDetail(null); setUpDown({ up: [], down: [] }); };

  // ---------- inspecting a node ----------
  useEffect(() => {
    const seq = ++detailSeq.current;
    setDetail(null); setDetailError(null); setUpDown({ up: [], down: [] });
    if (!selected) return;
    const node = graph?.nodes.find((n) => n.id === selected);
    if (!node) return;
    setDetailBusy(true);
    (async () => {
      try {
        const tr = await api.trace(viewTenant, selected);
        const ex = BELIEFS.has(node.label) ? await api.explain(viewTenant, selected) : null;
        if (seq !== detailSeq.current) return;
        logQ(`Trace ${selected}`, tr);
        if (ex) logQ(`Explain ${selected}`, ex);
        setUpDown({ up: tr.result.upstream.node_ids, down: tr.result.downstream.node_ids });
        setDetail({ supported: ex?.result.supported, explanation: ex?.result.explanation, agents: ex?.result.agents,
          upstream: tr.result.upstream.paths, downstream: tr.result.downstream.paths,
          truncated: tr.result.upstream.truncated || tr.result.downstream.truncated });
      } catch (e) { if (seq === detailSeq.current) setDetailError(explainError(e)); }
      finally { if (seq === detailSeq.current) setDetailBusy(false); }
    })();
  }, [selected, graph?.version, viewTenant, logQ]);   // eslint-disable-line react-hooks/exhaustive-deps

  const select = (id: string | null) => { setSelected(id); if (id) setTab("inspect"); };

  // ---------- marks shown on the graph ----------
  const marks = useMemo(() => {
    const m = emptyMarks();
    if (preview && viewTenant === workTenant) {
      preview.node_ids.forEach((i) => m.erase.add(i));
      preview.shared_derived_nodes.forEach((s) => m.shared.add(s.node_id));
      [...preview.kept_sources, ...preview.affected_memories.kept_with_other_content].forEach((i) => m.keep.add(i));
    }
    if (leaks) {
      for (const f of leaks.findings) {
        if (viewTenant === f.destination_tenant) {
          f.copies.forEach((c) => { m.leak.add(c.source_id); c.copy_nodes.forEach((n) => { m.leak.add(n.node_id); n.derived_node_ids.forEach((d) => m.leak.add(d)); }); });
        } else if (viewTenant === f.source_tenant) m.leak.add(f.source_fact_id);
      }
    }
    upDown.up.forEach((i) => m.up.add(i));
    upDown.down.forEach((i) => m.down.add(i));
    fresh.forEach((i) => m.fresh.add(i));
    return m;
  }, [preview, leaks, upDown, fresh, viewTenant, workTenant]);
  const focus = marks.erase.size + marks.up.size + marks.down.size + marks.leak.size > 0;

  const onAdded = useCallback((ids: string[]) => {
    setFresh(new Set(ids));
    window.setTimeout(() => setFresh(new Set()), 3500);
  }, []);

  // ---------- actions ----------
  const guard = async (name: string, setErr: (s: string | null) => void, fn: () => Promise<void>) => {
    setBusy(name); setErr(null);
    try { await fn(); } catch (e) { setErr(explainError(e)); } finally { setBusy(null); }
  };

  const onPreview = () => guard("preview", setEraseError, async () => {
    setReceipt(null); setAudit(null); setLeaks(null);
    try {
      const out = await api.preview(workTenant, userId, acting);
      logQ("Preview erasure", out); setPreview(out.result.preview);
    } catch (e) { setPreview(null); throw e; }
  });

  const onExecute = (reason: string) => guard("execute", setEraseError, async () => {
    if (!preview) return;
    const name = userName, tenant = workTenant, id = userId;
    try {
      const out = await api.execute(tenant, id, preview.preview_version, reason, acting);
      logQ("Erase", out);
      const r = out.result.receipt;
      setReceipt(r); setPreview(null);
      if (selected && r.deleted_node_ids.includes(selected)) setSelected(null);
      await Promise.all([loadGraph(viewTenant), loadConfig()]);
      try { const a = await api.audit(tenant, id, r.deleted_node_ids, [name]); logQ("Second audit", a); setAudit(a.result); } catch { /* shown by the receipt's own audit */ }
      try { const l = await api.leaks(tenant, config?.tenants.find((t) => t.id !== tenant)?.id ?? "globex"); logQ("Cross-tenant check", l); setLeaks(l.result); } catch { /* optional */ }
    } catch (e) {
      if (e instanceof ApiError && e.code === "PREVIEW_STALE" && e.details.new_preview) setPreview(e.details.new_preview as Preview);
      throw e;
    }
  });

  const onAddMemory = async (text: string): Promise<boolean> => {
    let ok = false;
    await guard("memory", setMemError, async () => {
      const out = await api.remember(workTenant, userId, text);
      logQ("Add memory", out); setLastMemory({ fact_id: out.result.fact_id, user: userId });
      await loadGraph(viewTenant);
      setSelected(out.result.fact_id); ok = true;
    });
    return ok;
  };

  const onAsk = (role: string, message: string) => guard("agents", setAgentError, async () => {
    setPipeline(null);
    const out = await api.ask(workTenant, userId, role, message);
    setAsked(out.response); out.response.cypher.length && setQueries((q) => [{ at: new Date().toLocaleTimeString(), source: `Ask ${role}`, cypher: out.response.cypher, params: [] }, ...q].slice(0, 60));
  });
  const onPipeline = (statements: string[]) => guard("agents", setAgentError, async () => {
    setAsked(null);
    const out = await api.pipeline(workTenant, userId, statements);
    setPipeline(out);
    await loadGraph(viewTenant);
  });

  const reset = async () => {
    setConfirmReset(false);
    try { await api.reset(); clearRun(); setSelected(null); setQueries([]); await Promise.all([loadConfig(), loadGraph(viewTenant)]); }
    catch (e) { setEraseError(explainError(e)); }
  };

  const node = graph?.nodes.find((n) => n.id === selected) ?? null;
  const labels = graph ? Object.keys(graph.counts.nodes) : [];
  const unseeded = config?.tenants.find((t) => t.id === viewTenant)?.seeded === false;

  return (
    <div className="app">
      <p className="banner" role="note">{config?.disclaimer ?? FALLBACK_DISCLAIMER}</p>
      <header className="top">
        <h1>Oo-blee-ET</h1>
        <div className="tenants" role="tablist" aria-label="Tenant">
          {(config?.tenants ?? [{ id: "acme" }, { id: "globex" }]).map((t) => (
            <button key={t.id} role="tab" type="button" aria-selected={viewTenant === t.id} onClick={() => chooseTenant(t.id)}>{t.id}</button>
          ))}
        </div>
        <div className="stats" aria-live="polite">
          {graph ? `${graph.nodes.length} nodes, ${graph.edges.length} edges` : ""}
        </div>
        {config?.demo_controls && (confirmReset ? (
          <span className="reset"><span>Replace all demo data?</span> <button type="button" className="danger" onClick={reset}>Reset</button> <button type="button" onClick={() => setConfirmReset(false)}>Cancel</button></span>
        ) : <button type="button" onClick={() => setConfirmReset(true)}>Reset demo data</button>)}
      </header>

      <main className="main">
        <section className="stage" aria-label="Graph">
          {viewTenant !== workTenant && <button type="button" className="backchip" onClick={() => setViewTenant(workTenant)}>Viewing {viewTenant}. Back to {workTenant}</button>}
          <GraphView graph={graph} marks={marks} focus={focus} selectedId={selected} showContext={showContext} onSelect={select} onAdded={onAdded} />
          {fatal && <div className="empty"><p>{fatal}</p><button type="button" onClick={() => void loadConfig().then(() => loadGraph(viewTenant))}>Try again</button></div>}
          {!fatal && !graph && (graphError || unseeded) && (
            <div className="empty"><p>{graphError ?? "This tenant has no graph yet."}</p>{config?.demo_controls && <button type="button" className="primary" onClick={reset}>Load demo data</button>}</div>
          )}
          {!fatal && !graph && !graphError && !unseeded && <div className="empty"><p>Loading the graph…</p></div>}
          <div className="legend">
            <ul>
              {labels.filter((l) => l !== "Tenant" || showContext || graph?.edges.some((e) => e.rel === "LEAKED_INTO")).map((l) => (
                <li key={l}><span className="swatch" style={{ background: palette.node[l] }} />{l} <span className="small">{graph?.counts.nodes[l]}</span></li>
              ))}
            </ul>
            <label className="check"><input type="checkbox" checked={showContext} onChange={(e) => setShowContext(e.target.checked)} /> Show memories and agents</label>
            <p className="small">Arrows point to what a node came from.</p>
          </div>
        </section>

        <aside className="panel" aria-label="Controls">
          <div className="tabs" role="tablist">
            {TABS.map((t) => <button key={t.id} role="tab" type="button" aria-selected={tab === t.id} onClick={() => setTab(t.id)}>{t.label}</button>)}
          </div>
          {tab === "erase" && (
            <ErasePanel tenant={workTenant} users={users} userId={userId} onUser={chooseUser} options={options} actingKey={actingKey}
              onActing={(k) => { setActingKey(k); setEraseError(null); }} graph={viewTenant === workTenant ? graph : null} preview={preview} receipt={receipt} audit={audit} leaks={leaks}
              busy={busy} error={eraseError} onPreview={onPreview} onExecute={onExecute} onShowTenant={setViewTenant} />
          )}
          {tab === "inspect" && <InspectPanel graph={graph} selectedId={selected} node={node} detail={detail} loading={detailBusy} error={detailError} onSelect={select} />}
          {tab === "memory" && <MemoryPanel users={users} userId={userId} onUser={chooseUser} busy={busy === "memory"} error={memError} last={lastMemory} onAdd={onAddMemory} />}
          {tab === "agents" && <AgentsPanel available={!!config?.llm_available} busy={busy === "agents"} error={agentError} asked={asked} pipeline={pipeline} onAsk={onAsk} onPipeline={onPipeline} />}
          {tab === "queries" && <QueryPanel entries={queries} />}
        </aside>
      </main>
    </div>
  );
}
