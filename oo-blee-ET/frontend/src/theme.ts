import type { StylesheetJson } from "cytoscape";

export interface Palette { dark: boolean; canvas: string; ink: string; muted: string; line: string; node: Record<string, string>; erase: string; keep: string; accent: string; leak: string; fresh: string }

export const light: Palette = {
  dark: false, canvas: "#e8edea", ink: "#172033", muted: "#5b6577", line: "#aab4bd",
  node: { User: "#172033", Fact: "#3a6ea5", Summary: "#2a8c82", Inference: "#7a5c9e", Action: "#c9822b", Source: "#6b7685", Memory: "#a8b3be", Agent: "#8e9aa8", Tenant: "#b7c0c8" },
  erase: "#c0392b", keep: "#b7791f", accent: "#1f5fbf", leak: "#a32a2a", fresh: "#16803c",
};
export const dark: Palette = {
  dark: true, canvas: "#10151d", ink: "#e6eaf0", muted: "#9aa5b5", line: "#46515f",
  node: { User: "#e6eaf0", Fact: "#6fa3d8", Summary: "#4fc0b4", Inference: "#b394d6", Action: "#e6a24f", Source: "#8a96a7", Memory: "#586373", Agent: "#6a7585", Tenant: "#46515f" },
  erase: "#ff6b5b", keep: "#e0a840", accent: "#7aa8ff", leak: "#ff7a7a", fresh: "#53d17f",
};

export const prefersDark = () => typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches;
export const reducedMotion = () => typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

const SHAPE: Record<string, string> = { User: "ellipse", Fact: "round-rectangle", Summary: "diamond", Inference: "hexagon", Action: "round-triangle", Source: "barrel", Memory: "round-rectangle", Agent: "tag", Tenant: "star" };
const SIZE: Record<string, number> = { User: 44, Fact: 34, Summary: 36, Inference: 36, Action: 36, Source: 32, Memory: 26, Agent: 26, Tenant: 34 };

export function stylesheet(p: Palette): StylesheetJson {
  const s: StylesheetJson = [
    { selector: "node", style: { "background-color": p.muted, "border-width": 2, "border-color": p.canvas, label: "data(caption)", color: p.ink, "font-size": 10, "font-family": "system-ui, sans-serif", "text-valign": "bottom", "text-margin-y": 5, "text-wrap": "ellipsis", "text-max-width": "120px", "min-zoomed-font-size": 7, "transition-property": "opacity", "transition-duration": 200 } },
    { selector: "edge", style: { width: 1.4, "line-color": p.line, "target-arrow-color": p.line, "target-arrow-shape": "triangle", "arrow-scale": 0.8, "curve-style": "bezier", opacity: 0.85 } },
    { selector: "edge[rel='ABOUT'], edge[rel='DERIVED_FROM']", style: { width: 1, "line-style": "dotted" } },
    { selector: "edge[rel='SUMMARIZES'], edge[rel='INFERRED_FROM'], edge[rel='BASED_ON']", style: { width: 2, "line-color": p.muted, "target-arrow-color": p.muted } },
    { selector: "edge[rel='SUPERSEDED_BY']", style: { "line-style": "dashed", "line-color": p.keep, "target-arrow-color": p.keep, "curve-style": "unbundled-bezier" } },
    { selector: "edge[rel='LEAKED_INTO']", style: { "line-style": "dashed", width: 2.4, "line-color": p.leak, "target-arrow-color": p.leak } },
    { selector: "edge[rel='CONTAINS'], edge[rel='WROTE']", style: { width: 0.8, opacity: 0.45 } },
    { selector: ".colhead", style: { width: 1, height: 1, "background-opacity": 0, "border-width": 0, label: "data(title)", "font-family": "'Iowan Old Style','Palatino Linotype',Palatino,Georgia,serif", "font-size": 14, color: p.muted, "text-valign": "center", "text-halign": "center", "text-wrap": "none", "text-max-width": "400px", events: "no", "min-zoomed-font-size": 0 } },
    { selector: "node:selected", style: { "border-color": p.accent, "border-width": 4 } },
    { selector: ".dim", style: { opacity: 0.28 } },
    { selector: ".up", style: { "border-color": p.accent, "border-width": 3 } },
    { selector: ".down", style: { "border-color": p.fresh, "border-width": 3 } },
    { selector: ".erase", style: { "border-color": p.erase, "border-width": 5, "underlay-color": p.erase, "underlay-opacity": 0.18, "underlay-padding": 7 } },
    { selector: ".shared", style: { "border-style": "double", "border-width": 6 } },
    { selector: ".keep", style: { "border-color": p.keep, "border-width": 4, "border-style": "dashed" } },
    { selector: ".leak", style: { "border-color": p.leak, "border-width": 5, "border-style": "double", "underlay-color": p.leak, "underlay-opacity": 0.14, "underlay-padding": 7 } },
    { selector: ".fresh", style: { "border-color": p.fresh, "border-width": 5 } },
    { selector: ".redacted", style: { "background-color": p.dark ? "#000" : "#0b0e14", "border-color": p.erase, "border-width": 4, color: "transparent", "text-opacity": 0, "transition-property": "opacity, background-color", "transition-duration": 450 } },
    { selector: ".gone", style: { opacity: 0 } },
  ];
  for (const [label, color] of Object.entries(p.node)) {
    s.push({ selector: `node[label='${label}']`, style: { "background-color": color, shape: SHAPE[label] as never, width: SIZE[label], height: SIZE[label] } });
  }
  s.push({ selector: "node[label='User']", style: { color: p.ink, "font-weight": 600 } });
  return s;
}
