/**
 * Enterprise knowledge-graph canvas for the Explore tab.
 *
 * Distinct from <GraphCanvas/> (the per-answer evidence *tree*, still on
 * react-flow — a small, already-open slice, a different job at a different
 * scale). This renders the *real* graph the /graph/overview endpoint
 * returns — document spine, typed facts grouped under their section,
 * cross-document identity hubs, and derived fact-to-fact edges — as a
 * node-link diagram, drawn with Cytoscape.js instead of react-flow.
 *
 * Why the switch: react-flow's "fit view" and zoom controls are DOM/CSS
 * driven and don't reliably compute a real bounding-box fit once a graph
 * spans several contract families — "fit" would snap to a fixed small zoom,
 * and zooming in with the +/- buttons could scroll the whole graph off
 * screen with nothing to click back to. Cytoscape's `fit()`/`zoom()` are
 * canvas-native primitives that don't have that failure mode, and its
 * class-based styling makes semantic zoom (below) a few lines instead of a
 * new subsystem. The actual LAYOUT MATH is unchanged — ELK was never the
 * problem, so it stays; only the drawing/interaction layer moved.
 *
 * Design goals (per the product North Star — explainability over features):
 *   • Legible by type — one colour family per node group, a legend.
 *   • Never a hairball — a layered, deterministic layout (doc → section →
 *     fact → hub) plus an expand-on-click model, now three levels deep:
 *       - Multiple contract families (the "all documents" scope): each
 *         family starts COLLAPSED into one labelled box with a document
 *         count. Click it open to reveal that family's real graph in place.
 *       - Within an open family: only the Document/Agreement spine renders
 *         at first, an Agreement opens to its Sections, a Section opens to
 *         its Facts.
 *     A single-family scope (the common case) skips the family layer
 *     entirely and behaves exactly like before.
 *   • Text disappears below a zoom threshold instead of rendering as
 *     unreadable pixel noise — zoom in, or open something, to read it.
 *   • Every node and edge comes from the store. Nothing is invented here,
 *     including the family boxes: a closed family's count and name are
 *     read off its own Document nodes, not guessed.
 *
 * Selection (node or edge) is lifted to the parent so the right-hand
 * explainability panel can show properties + provenance and offer a
 * "view in PDF" jump that reuses the existing citation/bbox machinery.
 * Clicking a family box never selects anything — it isn't a real node.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import cytoscape, { Core, EdgeSingular, ElementDefinition, NodeSingular } from "cytoscape";
import ELK, { ElkNode } from "elkjs/lib/elk.bundled.js";
import { GraphEdge, GraphNode, GraphPayload } from "../types";
import { groupForLabel, isProposalNode, labelsInGroup, nodeSearchText } from "./graphTheme";
import { IconFit, IconMinus, IconPlus } from "./Icon";


// ---------------------------------------------------------------------------
// Layering — assign every node a column so the layout reads left → right:
//   0 Document · 1 Agreement · 2 Section · 3 Fact · 4 hub / evidence / proposal
// ---------------------------------------------------------------------------

const isFactLabel = (label: string) => labelsInGroup("fact").has(label);

// The declared document-level DAG (pipeline/kb/intake.py's uploader relation),
// not a pack-declared edge, so it's a fixed set rather than legend-driven.
const CHAIN_RELS = new Set(["AMENDS", "SUPERSEDES", "NOVATES"]);

export type ViewMode = "full" | "lens";

function columnFor(label: string, mode: ViewMode = "full"): number {
  if (mode === "lens") {
    if (label === "Document" || label === "Agreement") return 0;
    return 1;
  }
  if (label === "Document") return 0;
  if (label === "Agreement") return 1;
  if (label === "Section") return 2;
  if (isFactLabel(label)) return 3;
  return 4; // identity hubs, evidence, proposals
}

const COL_X = [40, 280, 540, 860, 1200];
const ROW_H = 78;

// Rendered node footprint — also the literal cytoscape node size now, so
// there's one source of truth instead of a CSS pill react-flow approximated.
const NODE_W = 224;
const NODE_H = 58;

// A collapsed family's box — bigger and roomier than a real node, since its
// job is "read this from across the room," not "pack tightly."
const META_W = 260;
const META_H = 86;

const FAMILY_GAP_X = 120;
const FAMILY_GAP_Y = 100;

// Below this zoom level a real node's label is unreadable pixel noise, so it
// disappears instead of rendering as one. Family boxes are exempt — there
// are only ever a handful, and reading them from zoomed out is the point.
const LABEL_ZOOM_THRESHOLD = 0.55;

// Resolved concrete colours — a canvas renderer can't read CSS custom
// properties, same reason the old react-flow minimap kept its own hex map.
// Keys are the group ids GET /graph/legend can actually produce (see
// graphTheme.ts's FALLBACK) — this deliberately doesn't invent a group the
// legend never serves.
const GROUP_HEX: Record<string, string> = {
  document: "#8ab4ff",
  section: "#76a3ff",
  fact: "#4dd4ac",
  identity: "#ec4899",
  evidence: "#f59e0b",
  proposal: "#f87171",
  other: "#6c7280",
};


// ---------------------------------------------------------------------------
// ELK layered auto-layout — unchanged from the react-flow version. Pure
// position math, no DOM, no rendering dependency: it never needed to move.
// ---------------------------------------------------------------------------

const elk = new ELK();

const ELK_OPTIONS: Record<string, string> = {
  "elk.algorithm": "layered",
  "elk.direction": "RIGHT",
  "elk.partitioning.activate": "true",
  "elk.layered.spacing.nodeNodeBetweenLayers": "170",
  "elk.spacing.nodeNode": "26",
  "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
  "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
};

interface LayoutItem { id: string; col: number; w: number; h: number; }
type PosMap = Map<string, { x: number; y: number }>;

async function layoutElk(
  items: LayoutItem[],
  edges: { source: string; target: string }[],
): Promise<PosMap> {
  const graph: ElkNode = {
    id: "root",
    layoutOptions: ELK_OPTIONS,
    children: items.map(n => ({
      id: n.id,
      width: n.w,
      height: n.h,
      layoutOptions: { "elk.partitioning.partition": String(n.col) },
    })),
    edges: edges.map((e, i) => ({ id: `e${i}`, sources: [e.source], targets: [e.target] })),
  };
  const res = await elk.layout(graph);
  const out: PosMap = new Map();
  for (const c of res.children ?? []) {
    if (Number.isFinite(c.x) && Number.isFinite(c.y)) {
      out.set(c.id, { x: c.x as number, y: c.y as number });
    }
  }
  return out;
}


// ---------------------------------------------------------------------------
// Contract families — the "all documents" scope spans every independent
// amendment chain, with no edges between them beyond an occasional shared
// identity hub. Every node (real or not-yet-visible) belongs to exactly one
// family, walked outward from each Document's own `group` prop (or its own
// id, ungrouped = its own family — the same rule the KB uses).
// ---------------------------------------------------------------------------

function computeFamilies(nodes: GraphNode[], edges: GraphEdge[]): Map<string, string> {
  const adj = new Map<string, string[]>();
  const push = (a: string, b: string) => {
    const list = adj.get(a);
    if (list) list.push(b); else adj.set(a, [b]);
  };
  for (const e of edges) { push(e.source, e.target); push(e.target, e.source); }

  const family = new Map<string, string>();
  const docs = nodes
    .filter(n => n.label === "Document")
    .sort((a, b) => a.id.localeCompare(b.id)); // deterministic claim order

  for (const d of docs) {
    if (family.has(d.id)) continue;
    const key = (d.props?.["group"] as string) || d.id;
    const queue = [d.id];
    family.set(d.id, key);
    while (queue.length) {
      const cur = queue.shift() as string;
      for (const nb of adj.get(cur) ?? []) {
        if (!family.has(nb)) { family.set(nb, key); queue.push(nb); }
      }
    }
  }
  for (const n of nodes) if (!family.has(n.id)) family.set(n.id, n.id);
  return family;
}

interface FamilyInfo { key: string; title: string; count: number; }

/** One entry per family: the count of its documents, and a representative
 * title — the document that never AMENDS/SUPERSEDES/NOVATES another one in
 * the same family (the base agreement), or the first document if the chain
 * relation isn't present. Read off the data, never guessed. */
function computeFamilyInfo(payload: GraphPayload, familyOf: Map<string, string>): Map<string, FamilyInfo> {
  const byFamily = new Map<string, GraphNode[]>();
  for (const n of payload.nodes) {
    if (n.label !== "Document") continue;
    const f = familyOf.get(n.id) as string;
    const list = byFamily.get(f);
    if (list) list.push(n); else byFamily.set(f, [n]);
  }
  const chainSources = new Set(
    payload.edges.filter(e => CHAIN_RELS.has(e.type)).map(e => e.source),
  );
  const out = new Map<string, FamilyInfo>();
  for (const [key, docs] of byFamily) {
    const root = docs.find(d => !chainSources.has(d.id)) ?? docs[0];
    out.set(key, { key, title: root.title, count: docs.length });
  }
  return out;
}


// ---------------------------------------------------------------------------
// Visibility — within an OPEN family, only the Document/Agreement spine
// shows by default; Sections show once their Agreement is opened, Facts
// once their Section is opened. Identical to the pre-cytoscape model, just
// extracted to a plain function instead of a react-flow node builder.
// ---------------------------------------------------------------------------

function parentMap(payload: GraphPayload, edgeType: string, parentLabel: string): Map<string, string> {
  const byId = new Map(payload.nodes.map(n => [n.id, n]));
  const m = new Map<string, string>();
  for (const e of payload.edges) {
    if (e.type !== edgeType) continue;
    const src = byId.get(e.source);
    if (src?.label === parentLabel) m.set(e.target, e.source);
  }
  return m;
}

function fineVisible(
  payload: GraphPayload,
  openSections: Set<string>,
  openAgreements: Set<string>,
  hiddenGroups: Set<string>,
  mode: ViewMode,
): Set<string> {
  const byId = new Map(payload.nodes.map(n => [n.id, n]));
  const agreementOfSection = parentMap(payload, "HAS_SECTION", "Agreement");
  const sectionOfFact = parentMap(payload, "IN_SECTION", "Section");
  const anyAgreementLink = agreementOfSection.size > 0;
  const anySectionLink = sectionOfFact.size > 0;
  const groupVisible = (n: GraphNode) => !hiddenGroups.has(groupForLabel(n.label));

  const visible = new Set<string>();
  for (const n of payload.nodes) {
    if (!groupVisible(n)) continue;
    const col = columnFor(n.label, mode);
    if (col <= 1) { visible.add(n.id); continue; }
    if (col === 2) {
      const agr = agreementOfSection.get(n.id);
      if (!anyAgreementLink || !agr || openAgreements.has(agr)) visible.add(n.id);
      continue;
    }
    if (isFactLabel(n.label)) {
      const sec = sectionOfFact.get(n.id);
      if (!anySectionLink || !sec || openSections.has(sec)) visible.add(n.id);
      continue;
    }
    // Hubs / evidence / proposals: deferred to the second pass below.
  }

  for (const e of payload.edges) {
    const a = byId.get(e.source);
    const b = byId.get(e.target);
    if (!a || !b) continue;
    if (visible.has(e.source) && columnFor(b.label, mode) === 4 && groupVisible(b)) visible.add(e.target);
    if (visible.has(e.target) && columnFor(a.label, mode) === 4 && groupVisible(a)) visible.add(e.source);
  }
  return visible;
}


// ---------------------------------------------------------------------------
// Build the element data (no positions yet) for the current state: which
// families are open, which agreements/sections within them are open, what
// matched a search. A closed family collapses to one meta node; any edge
// that would have touched something inside it reroutes to that meta node
// instead (deduped), so a genuine cross-family link stays visible instead
// of silently vanishing the way an edge to a hidden node used to.
// ---------------------------------------------------------------------------

interface NodeDatum {
  id: string;
  col: number;          // layout column — real column, or 0 for a family box
  familyKey: string;
  isFamily: boolean;
  raw?: GraphNode;       // absent for a family box
  rawLabel?: string;     // node label, for the Agreement/Section click toggle
  kicker: string;        // small type line ("DOCUMENT", or the family's own line)
  title: string;
  group: string;
  proposal: boolean;
  count?: number;        // family box only
  highlight?: boolean;   // true = search match, false = dimmed, undefined = no search
}

interface EdgeDatum {
  id: string;
  source: string;
  target: string;
  kind: "identity" | "chain" | "structural";
  edgeLabel: string;
  raw?: GraphEdge;        // absent once deduped across a rerouted meta edge
}

interface BuiltGraph {
  nodeDefs: NodeDatum[];
  edgeDefs: EdgeDatum[];
}

function shortTitle(t: string): string {
  const s = (t || "").replace(/\s+/g, " ").trim();
  return s.length > 60 ? s.slice(0, 59) + "…" : s;
}

function buildGraph(
  payload: GraphPayload,
  openSections: Set<string>,
  openAgreements: Set<string>,
  openFamilies: Set<string>,
  hiddenGroups: Set<string>,
  mode: ViewMode,
  matchIds: Set<string>,
): BuiltGraph {
  const searching = matchIds.size > 0;
  const byId = new Map(payload.nodes.map(n => [n.id, n]));
  const familyOf = computeFamilies(payload.nodes, payload.edges);
  const families = [...new Set(familyOf.values())];
  const familyInfo = computeFamilyInfo(payload, familyOf);
  // A single family (the common, single-contract scope) never collapses —
  // there is nothing to hide behind a box for.
  const effectivelyOpen = families.length <= 1 ? new Set(families) : openFamilies;

  const realVisible = fineVisible(payload, openSections, openAgreements, hiddenGroups, mode);
  const docsHidden = hiddenGroups.has("document");

  const nodeDefs: NodeDatum[] = [];
  for (const n of payload.nodes) {
    const fam = familyOf.get(n.id) as string;
    if (!effectivelyOpen.has(fam)) continue; // lives inside a collapsed family box instead
    if (!realVisible.has(n.id)) continue;
    const group = groupForLabel(n.label);
    nodeDefs.push({
      id: n.id,
      col: columnFor(n.label, mode),
      familyKey: fam,
      isFamily: false,
      raw: n,
      rawLabel: n.label,
      kicker: n.label.toUpperCase(),
      title: shortTitle(n.title),
      group,
      proposal: isProposalNode(n),
      highlight: searching ? matchIds.has(n.id) : undefined,
    });
  }
  if (!docsHidden) {
    for (const fam of families) {
      if (effectivelyOpen.has(fam)) continue;
      const info = familyInfo.get(fam);
      if (!info) continue; // a family with no Document node at all (shouldn't happen)
      nodeDefs.push({
        id: `family:${fam}`,
        col: 0,
        familyKey: fam,
        isFamily: true,
        kicker: "CONTRACT FAMILY",
        title: shortTitle(info.title),
        group: "document",
        proposal: false,
        count: info.count,
      });
    }
  }

  const resolve = (id: string): string | null => {
    if (realVisible.has(id) && effectivelyOpen.has(familyOf.get(id) as string)) return id;
    const fam = familyOf.get(id);
    if (fam && !effectivelyOpen.has(fam) && !docsHidden) return `family:${fam}`;
    return null;
  };

  const seen = new Set<string>();
  const edgeDefs: EdgeDatum[] = [];
  for (const e of payload.edges) {
    const s = resolve(e.source);
    const t = resolve(e.target);
    if (!s || !t || s === t) continue;
    const isReal = s === e.source && t === e.target;
    const isIdentity = e.type === "RESOLVES_TO";
    const isChain = CHAIN_RELS.has(e.type);
    const kind: EdgeDatum["kind"] = isIdentity ? "identity" : isChain ? "chain" : "structural";
    const key = `${e.type}|${s}|${t}`;
    if (seen.has(key)) continue;
    seen.add(key);
    edgeDefs.push({
      id: key,
      source: s,
      target: t,
      kind,
      edgeLabel: e.type.replace(/_/g, " ").toLowerCase(),
      raw: isReal ? e : undefined,
    });
  }

  return { nodeDefs, edgeDefs };
}


// ---------------------------------------------------------------------------
// Positions — one independent layout per open block (a real family's
// subgraph, laid out with ELK same as ever), then every block (real or a
// single fixed-size family box) is grid-packed. A single family collapses
// to plain layoutElk with zero grid overhead, exactly as before.
// ---------------------------------------------------------------------------

function bbox(items: { x: number; y: number; w: number; h: number }[]) {
  if (!items.length) return { minX: 0, minY: 0, w: NODE_W, h: NODE_H };
  const minX = Math.min(...items.map(i => i.x));
  const minY = Math.min(...items.map(i => i.y));
  const maxX = Math.max(...items.map(i => i.x + i.w));
  const maxY = Math.max(...items.map(i => i.y + i.h));
  return { minX, minY, w: maxX - minX, h: maxY - minY };
}

async function layoutFamilyGrid(nodeDefs: NodeDatum[], edgeDefs: EdgeDatum[]): Promise<PosMap> {
  const byFamily = new Map<string, NodeDatum[]>();
  for (const n of nodeDefs) {
    const list = byFamily.get(n.familyKey);
    if (list) list.push(n); else byFamily.set(n.familyKey, [n]);
  }
  const familyKeys = [...byFamily.keys()];

  if (familyKeys.length <= 1) {
    const only = familyKeys[0] ? byFamily.get(familyKeys[0])! : [];
    const items: LayoutItem[] = only.map(n => ({ id: n.id, col: n.col, w: n.isFamily ? META_W : NODE_W, h: n.isFamily ? META_H : NODE_H }));
    const idSet = new Set(only.map(n => n.id));
    const edges = edgeDefs.filter(e => idSet.has(e.source) && idSet.has(e.target));
    return layoutElk(items, edges);
  }

  const blocks = await Promise.all(familyKeys.map(async key => {
    const nodes = byFamily.get(key)!;
    if (nodes.length === 1 && nodes[0].isFamily) {
      return { key, positions: new Map([[nodes[0].id, { x: 0, y: 0 }]]) as PosMap, w: META_W, h: META_H };
    }
    const idSet = new Set(nodes.map(n => n.id));
    const items: LayoutItem[] = nodes.map(n => ({ id: n.id, col: n.col, w: NODE_W, h: NODE_H }));
    const edges = edgeDefs.filter(e => idSet.has(e.source) && idSet.has(e.target));
    const positions = await layoutElk(items, edges);
    const box = bbox(nodes.map(n => {
      const p = positions.get(n.id) ?? { x: 0, y: 0 };
      return { x: p.x, y: p.y, w: NODE_W, h: NODE_H };
    }));
    return { key, positions, w: box.w, h: box.h, minX: box.minX, minY: box.minY };
  }));

  const cols = Math.max(1, Math.ceil(Math.sqrt(blocks.length)));
  const cellW = Math.max(...blocks.map(b => b.w)) + FAMILY_GAP_X;
  const cellH = Math.max(...blocks.map(b => b.h)) + FAMILY_GAP_Y;

  const out: PosMap = new Map();
  blocks.forEach((b, i) => {
    const dx = (i % cols) * cellW - (b.minX ?? 0);
    const dy = Math.floor(i / cols) * cellH - (b.minY ?? 0);
    for (const [id, p] of b.positions) out.set(id, { x: p.x + dx, y: p.y + dy });
  });
  return out;
}

/** Instant first-frame placement while the real (async) layout is pending —
 * a plain top-to-bottom stack per column, never blank, never NaN. */
function deterministicPositions(nodeDefs: NodeDatum[]): PosMap {
  const cursor: Record<number, number> = {};
  const out: PosMap = new Map();
  for (const n of nodeDefs) {
    const row = (cursor[n.col] = (cursor[n.col] ?? 0) + 1);
    out.set(n.id, { x: COL_X[n.col] ?? COL_X[COL_X.length - 1], y: row * (n.isFamily ? META_H + 16 : ROW_H) });
  }
  return out;
}


// ---------------------------------------------------------------------------
// Cytoscape stylesheet — resolved colours, function-valued per element data
// instead of a class per colour. `any` casts are because @types/cytoscape's
// own header calls its stylesheet types "provisional" and doesn't model
// function-valued properties precisely.
// ---------------------------------------------------------------------------

/* eslint-disable @typescript-eslint/no-explicit-any */
const STYLESHEET: any[] = [
  {
    selector: "node",
    style: {
      shape: "round-rectangle",
      width: NODE_W,
      height: NODE_H,
      "background-color": "#1c1d23",
      "border-width": 2,
      "border-color": (ele: NodeSingular) => GROUP_HEX[ele.data("group") as string] || GROUP_HEX.other,
      "border-style": (ele: NodeSingular) => (ele.data("proposal") ? "dashed" : "solid"),
      // A precomputed field read through the same plain data() mapper edge
      // labels already use, not a style function returning a template
      // string — the function form was observed, production build only, to
      // leave every node unlabelled while shapes and edge labels (which use
      // data()) drew fine. Same information, the mapper form is proven.
      label: "data(displayLabel)",
      "text-wrap": "wrap",
      "text-max-width": NODE_W - 24,
      "text-valign": "center",
      "text-halign": "center",
      "font-size": 10.5,
      "line-height": 1.35,
      color: "#ecedf1",
      "overlay-opacity": 0,
      "transition-property": "border-width, overlay-opacity",
      "transition-duration": 90 as unknown as string,
    },
  },
  {
    selector: "node.cynode--family",
    style: {
      width: META_W,
      height: META_H,
      "background-color": "rgba(138, 180, 255, 0.08)",
      "border-color": "#8ab4ff",
      "border-width": 2,
      "border-style": "dashed",
      "font-size": 12,
      "font-weight": 600,
      "text-max-width": META_W - 28,
    },
  },
  {
    selector: "node.cynode--hover",
    style: { "border-width": 3 },
  },
  {
    selector: "node.cynode--selected",
    style: { "overlay-color": "#5b8def", "overlay-opacity": 0.28, "overlay-padding": 5 },
  },
  {
    selector: "node.cynode--match",
    style: { "overlay-color": "#f59e0b", "overlay-opacity": 0.32, "overlay-padding": 5 },
  },
  {
    selector: "node.cynode--dim",
    style: { opacity: 0.35 },
  },
  {
    selector: "node.cynode--zoomed-out",
    style: { "text-opacity": 0 },
  },
  {
    selector: "edge",
    style: {
      width: (ele: EdgeSingular) => (ele.data("kind") === "chain" ? 1.8 : ele.data("kind") === "identity" ? 1.6 : 1.1),
      "line-color": (ele: EdgeSingular) =>
        ele.data("kind") === "chain" ? "#f59e0b" : ele.data("kind") === "identity" ? "#4dd4ac" : "rgba(255,255,255,0.28)",
      "line-style": (ele: EdgeSingular) => (ele.data("kind") === "identity" ? "dashed" : "solid"),
      "curve-style": "bezier",
      "target-arrow-shape": "none",
      label: "data(edgeLabel)",
      "font-size": 9,
      color: "#9da3ae",
      "text-background-color": "#16171c",
      "text-background-opacity": 0.85,
      "text-background-shape": "roundrectangle",
      "text-background-padding": 2,
    },
  },
];
/* eslint-enable @typescript-eslint/no-explicit-any */


// ---------------------------------------------------------------------------
// Public component
// ---------------------------------------------------------------------------

export interface ExploreCanvasProps {
  payload: GraphPayload;
  hiddenGroups: Set<string>;
  selectedNodeId: string | null;
  onSelectNode: (n: GraphNode | null) => void;
  onSelectEdge: (e: GraphEdge | null) => void;
  /** "full" (whole graph, expand-on-click) or "lens" (cross-doc hubs only). */
  mode?: ViewMode;
  /** Free-text filter — matches highlight, everything else dims. Empty/unset
   * means no search is running (no highlight, no dim). */
  search?: string;
}

export function ExploreCanvas({
  payload, hiddenGroups, selectedNodeId, onSelectNode, onSelectEdge,
  mode = "full", search = "",
}: ExploreCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const lastIdsRef = useRef<Set<string>>(new Set());

  // Every click handler below is registered ONCE (cy is created once) but
  // needs the latest React state/props — a ref indirection avoids either
  // stale closures or tearing the cy instance down and rebuilding it every
  // render just to rebind event handlers.
  const [openAgreements, setOpenAgreements] = useState<Set<string>>(new Set());
  const [openSections, setOpenSections] = useState<Set<string>>(new Set());
  const [openFamilies, setOpenFamilies] = useState<Set<string>>(new Set());

  const needle = search.trim().toLowerCase();
  const matchIds = useMemo(() => {
    if (!needle) return new Set<string>();
    const m = new Set<string>();
    for (const n of payload.nodes) if (nodeSearchText(n).includes(needle)) m.add(n.id);
    return m;
  }, [payload, needle]);

  // A match hiding behind a closed section, agreement, or family must force
  // all three open — a hit sitting invisible behind a collapsed box is worse
  // than no search at all.
  const forcedOpen = useMemo(() => {
    const sections = new Set<string>();
    const agreements = new Set<string>();
    const families = new Set<string>();
    if (!matchIds.size) return { sections, agreements, families };
    const byId = new Map(payload.nodes.map(n => [n.id, n]));
    const familyOf = computeFamilies(payload.nodes, payload.edges);
    const agreementOfSection = parentMap(payload, "HAS_SECTION", "Agreement");
    const sectionOfFact = parentMap(payload, "IN_SECTION", "Section");
    for (const id of matchIds) {
      const fam = familyOf.get(id);
      if (fam) families.add(fam);
      if (byId.get(id)?.label === "Section") {
        const agr = agreementOfSection.get(id);
        if (agr) agreements.add(agr);
      }
      const sec = sectionOfFact.get(id);
      if (sec) {
        sections.add(sec);
        const agr = agreementOfSection.get(sec);
        if (agr) agreements.add(agr);
      }
    }
    return { sections, agreements, families };
  }, [matchIds, payload]);

  const effectiveOpenSections = useMemo(
    () => forcedOpen.sections.size ? new Set([...openSections, ...forcedOpen.sections]) : openSections,
    [openSections, forcedOpen],
  );
  const effectiveOpenAgreements = useMemo(
    () => forcedOpen.agreements.size ? new Set([...openAgreements, ...forcedOpen.agreements]) : openAgreements,
    [openAgreements, forcedOpen],
  );
  const effectiveOpenFamilies = useMemo(
    () => forcedOpen.families.size ? new Set([...openFamilies, ...forcedOpen.families]) : openFamilies,
    [openFamilies, forcedOpen],
  );

  const base = useMemo(
    () => buildGraph(payload, effectiveOpenSections, effectiveOpenAgreements, effectiveOpenFamilies, hiddenGroups, mode, matchIds),
    [payload, effectiveOpenSections, effectiveOpenAgreements, effectiveOpenFamilies, hiddenGroups, mode, matchIds],
  );

  const nFamilies = useMemo(() => new Set(computeFamilies(payload.nodes, payload.edges).values()).size, [payload]);

  const sig = useMemo(
    () => base.nodeDefs.map(n => n.id).join("|") + "##" + base.edgeDefs.map(e => e.id).join("|"),
    [base],
  );

  const [laid, setLaid] = useState<{ sig: string; positions: PosMap }>({ sig: "", positions: new Map() });
  const layoutSeq = useRef(0);
  useEffect(() => {
    const seq = ++layoutSeq.current;
    if (base.nodeDefs.length === 0) { setLaid({ sig, positions: new Map() }); return; }
    layoutFamilyGrid(base.nodeDefs, base.edgeDefs)
      .then(positions => { if (seq === layoutSeq.current) setLaid({ sig, positions }); })
      .catch(() => { if (seq === layoutSeq.current) setLaid({ sig, positions: deterministicPositions(base.nodeDefs) }); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);

  const elements = useMemo((): ElementDefinition[] => {
    const positions = laid.sig === sig ? laid.positions : deterministicPositions(base.nodeDefs);
    const nodeEls: ElementDefinition[] = base.nodeDefs.map(n => {
      const classes = [
        n.isFamily && "cynode--family",
        n.id === selectedNodeId && "cynode--selected",
        n.highlight === true && "cynode--match",
        n.highlight === false && "cynode--dim",
      ].filter(Boolean).join(" ");
      const p = positions.get(n.id) ?? { x: 0, y: 0 };
      const title = n.isFamily ? `${n.title} (${n.count})` : n.title;
      return {
        data: {
          id: n.id,
          group: n.group,
          proposal: n.proposal,
          kicker: n.kicker,
          title,
          displayLabel: `${n.kicker}\n${title}`,
          isFamily: n.isFamily,
          familyKey: n.familyKey,
          rawLabel: n.rawLabel,
          raw: n.raw,
        },
        position: p,
        classes,
        selectable: false, // selection is a class we drive ourselves, see below
        grabbable: !n.isFamily,
      };
    });
    const edgeEls: ElementDefinition[] = base.edgeDefs.map(e => ({
      data: {
        id: e.id, source: e.source, target: e.target,
        kind: e.kind, edgeLabel: e.edgeLabel, raw: e.raw,
      },
      selectable: false,
    }));
    return [...nodeEls, ...edgeEls];
  }, [base, laid, sig, selectedNodeId]);

  // --- cy lifecycle: created once, destroyed on unmount --------------------
  const onNodeTapRef = useRef<(n: NodeSingular) => void>(() => {});
  const onEdgeTapRef = useRef<(e: EdgeSingular) => void>(() => {});
  const onPaneTapRef = useRef<() => void>(() => {});

  onNodeTapRef.current = (node: NodeSingular) => {
    const d = node.data();
    if (d.isFamily) {
      setOpenFamilies(curr => new Set([...curr, d.familyKey]));
      return;
    }
    onSelectEdge(null);
    onSelectNode(d.raw as GraphNode);
    if (d.rawLabel === "Agreement") {
      setOpenAgreements(curr => {
        const next = new Set(curr);
        if (next.has(node.id())) next.delete(node.id()); else next.add(node.id());
        return next;
      });
    } else if (d.rawLabel === "Section") {
      setOpenSections(curr => {
        const next = new Set(curr);
        if (next.has(node.id())) next.delete(node.id()); else next.add(node.id());
        return next;
      });
    }
  };
  onEdgeTapRef.current = (edge: EdgeSingular) => {
    const raw = edge.data("raw") as GraphEdge | undefined;
    if (raw) { onSelectNode(null); onSelectEdge(raw); }
  };
  onPaneTapRef.current = () => { onSelectNode(null); onSelectEdge(null); };

  useEffect(() => {
    if (!containerRef.current) return;
    const cy = cytoscape({
      container: containerRef.current,
      style: STYLESHEET,
      minZoom: 0.08,
      maxZoom: 2.5,
      wheelSensitivity: 0.22,
      boxSelectionEnabled: false,
      autounselectify: true,
    });
    cyRef.current = cy;

    cy.on("tap", "node", evt => onNodeTapRef.current(evt.target));
    cy.on("tap", "edge", evt => onEdgeTapRef.current(evt.target));
    cy.on("tap", evt => { if (evt.target === cy) onPaneTapRef.current(); });
    cy.on("mouseover", "node", evt => evt.target.addClass("cynode--hover"));
    cy.on("mouseout", "node", evt => evt.target.removeClass("cynode--hover"));
    cy.on("zoom", () => {
      const zoomedOut = cy.zoom() < LABEL_ZOOM_THRESHOLD;
      cy.nodes().not(".cynode--family").toggleClass("cynode--zoomed-out", zoomedOut);
    });

    const ro = new ResizeObserver(() => cy.resize());
    ro.observe(containerRef.current);

    return () => { ro.disconnect(); cy.destroy(); cyRef.current = null; lastIdsRef.current = new Set(); };
  }, []);

  // --- sync computed elements into the live cy instance ---------------------
  // Three distinct triggers, not two: the node/edge ID SET changing (a real
  // topology change — rebuild) is separate from the ASYNC layout for that
  // same topology finishing a beat later (reposition in place). Collapsing
  // those into one "did the id set change" check was the bug: the first
  // paint after any topology change uses deterministicPositions (instant,
  // synchronous), and by the time the real ELK/grid positions resolve the id
  // set is already identical to what's on screen — so the old single check
  // took the cheap data-only path and silently kept the placeholder layout
  // forever. A plain style/selection change (the third case) still only
  // restyles, so pan/zoom the user did in between is never fought.
  const lastLaidSigRef = useRef("");
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const ids = new Set(elements.map(el => el.data.id as string));
    const idsChanged = ids.size !== lastIdsRef.current.size || [...ids].some(id => !lastIdsRef.current.has(id));
    const layoutJustResolved = laid.sig === sig && lastLaidSigRef.current !== sig;
    if (layoutJustResolved) lastLaidSigRef.current = sig;

    // A container inside a flex layout can still be mid-resize (e.g. right
    // after a tab switch) when this effect runs — resize() re-reads the
    // container's real box immediately rather than waiting on the async
    // ResizeObserver to get there first, so fit() computes against current
    // dimensions instead of whatever the container happened to be at mount.
    const doFit = () => { cy.resize(); cy.fit(undefined, 48); };

    if (idsChanged) {
      // Not wrapped in cy.batch(): a remove-then-add of the same ids inside
      // one batch has been observed, only in a production (minified) build,
      // to leave the readded elements in the model (data/style/position all
      // compute correctly) but never registered with the renderer, so they
      // silently never draw — no error, nothing to catch, just a permanently
      // blank canvas. Unbatched add/remove is a hair slower with no visible
      // cost at this graph's size, and it always renders.
      cy.elements().remove();
      cy.add(elements);
      cy.layout({ name: "preset", fit: false } as cytoscape.LayoutOptions).run();
      lastIdsRef.current = ids;
      requestAnimationFrame(doFit);
      return;
    }
    for (const el of elements) {
      const ele = cy.getElementById(el.data.id as string);
      if (ele.empty()) continue;
      ele.data(el.data);
      ele.classes(el.classes ?? "");
      if (layoutJustResolved && el.position) ele.position(el.position as cytoscape.Position);
    }
    if (layoutJustResolved) requestAnimationFrame(doFit);
  }, [elements, laid.sig, sig]);

  const zoomBy = (factor: number) => {
    const cy = cyRef.current;
    if (!cy) return;
    // Anchor on the current viewport centre, not a fixed point — this is
    // exactly what react-flow's zoom buttons didn't do, letting a graph
    // scroll off screen after a couple of clicks.
    cy.zoom({
      level: Math.max(cy.minZoom(), Math.min(cy.maxZoom(), cy.zoom() * factor)),
      renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 },
    });
  };
  const fit = () => { const cy = cyRef.current; if (!cy) return; cy.resize(); cy.fit(undefined, 48); };

  const showCollapseAll = nFamilies > 1 && effectiveOpenFamilies.size > 0;

  return (
    <div className="rfgraph rfgraph--explore cygraph">
      <div ref={containerRef} className="cygraph__stage" />
      {payload.nodes.length === 0 && (
        <div className="rfgraph__empty">No graph data for this scope.</div>
      )}
      <div className="cygraph__controls">
        <button type="button" onClick={() => zoomBy(1.35)} title="Zoom in" aria-label="Zoom in">
          <IconPlus size={14} />
        </button>
        <button type="button" onClick={() => zoomBy(1 / 1.35)} title="Zoom out" aria-label="Zoom out">
          <IconMinus size={14} />
        </button>
        <button type="button" onClick={fit} title="Fit to screen" aria-label="Fit to screen">
          <IconFit size={14} />
        </button>
        {showCollapseAll && (
          <button
            type="button"
            className="cygraph__controls-text"
            onClick={() => setOpenFamilies(new Set())}
            title="Close every opened contract family box"
          >
            Collapse all
          </button>
        )}
      </div>
    </div>
  );
}
