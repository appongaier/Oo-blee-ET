import cytoscape, { type Core } from "cytoscape";
import { useEffect, useRef } from "react";
import { applyMarks, syncGraph, type Marks } from "./graphCore";
import { dark, light, prefersDark, reducedMotion, stylesheet } from "./theme";
import { useDark } from "./useDark";
import type { GraphData } from "./types";

interface Props {
  graph: GraphData | null;
  marks: Marks;
  focus: boolean;
  selectedId: string | null;
  showContext: boolean;
  onSelect: (id: string | null) => void;
  onAdded: (ids: string[]) => void;
}

export function GraphView({ graph, marks, focus, selectedId, showContext, onSelect, onAdded }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const lastTenant = useRef<string | null>(null);
  const select = useRef(onSelect);
  select.current = onSelect;
  const isDark = useDark();

  useEffect(() => {
    const cy = cytoscape({ container: host.current, elements: [], style: stylesheet(prefersDark() ? dark : light), minZoom: 0.15, maxZoom: 2.5, wheelSensitivity: 0.25, boxSelectionEnabled: false });
    cy.on("tap", "node", (e) => { if (!e.target.hasClass("colhead")) select.current(e.target.id()); });
    cy.on("tap", (e) => { if (e.target === cy) select.current(null); });
    cyRef.current = cy;
    return () => { cy.destroy(); cyRef.current = null; };
  }, []);

  useEffect(() => { cyRef.current?.style(stylesheet(isDark ? dark : light)); }, [isDark]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !graph) return;
    const tenantChanged = lastTenant.current !== graph.tenant;
    const res = syncGraph(cy, graph, showContext, { animate: !reducedMotion() && !tenantChanged, fit: tenantChanged });
    lastTenant.current = graph.tenant;
    if (!tenantChanged && res.added.length) onAdded(res.added);
    applyMarks(cy, marks, selectedId, focus);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph, showContext]);

  useEffect(() => { if (cyRef.current) applyMarks(cyRef.current, marks, selectedId, focus); }, [marks, selectedId, focus]);

  const zoom = (f: number) => { const cy = cyRef.current; if (cy) cy.zoom({ level: cy.zoom() * f, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } }); };
  const fit = () => cyRef.current?.fit(cyRef.current.elements().not(".gone"), 48);

  return (
    <div className="graph-wrap">
      <div ref={host} className="graph" role="img" aria-label={graph ? `Provenance graph of tenant ${graph.tenant}: ${graph.nodes.length} nodes and ${graph.edges.length} edges` : "Provenance graph"} />
      <div className="graph-tools" role="group" aria-label="Graph view controls">
        <button type="button" onClick={() => zoom(1.25)} aria-label="Zoom in">+</button>
        <button type="button" onClick={() => zoom(0.8)} aria-label="Zoom out">−</button>
        <button type="button" onClick={fit}>Fit</button>
      </div>
    </div>
  );
}
