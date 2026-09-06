/**
 * Enterprise knowledge-graph canvas for the Explore tab.
 *
 * Distinct from <GraphCanvas/> (the per-answer evidence *tree*). This renders
 * the *real* graph the /graph/overview endpoint returns — document spine,
 * typed facts grouped under their section, cross-document identity hubs, and
 * derived fact-to-fact edges — as a node-link diagram.
 *
 * Design goals (per the product North Star — explainability over features):
 *   • Legible by type — one colour/shape family per node label, a legend.
 *   • Never a hairball — a layered, deterministic layout (doc → section →
 *     fact → hub) plus an expand-on-click model: only the Document/Agreement
 *     spine renders at first, an Agreement opens to its Sections, a Section
 *     opens to its Facts — two clicks deep, nothing else, until asked for.
 *   • Every node and edge comes from the store. Nothing is invented here.
 *
 * Selection (node or edge) is lifted to the parent so the right-hand
 * explainability panel can show properties + provenance and offer a
 * "view in PDF" jump that reuses the existing citation/bbox machinery.
 */
import { Dispatch, SetStateAction, useEffect, useMemo, useRef, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  Edge,
  Handle,
  MiniMap,
  Node,
  NodeProps,
  Position,
  ReactFlowProvider,
  useReactFlow,
} from "reactflow";
import "reactflow/dist/style.css";
import ELK, { ElkNode } from "elkjs/lib/elk.bundled.js";
import { GraphEdge, GraphNode, GraphPayload } from "../types";
import {
  groupColorVar,
  groupForLabel,
  isProposalNode,
  labelsInGroup,
  nodeSearchText,
} from "./graphTheme";


// ---------------------------------------------------------------------------
// Layering — assign every node a column so the layout reads left → right:
//   0 Document · 1 Agreement · 2 Section · 3 Fact · 4 hub / evidence / proposal
// ---------------------------------------------------------------------------

// Read at call time, not at module load: the legend arrives from the API.
const isFactLabel = (label: string) => labelsInGroup("fact").has(label);

// The declared document-level DAG (pipeline/kb/intake.py's uploader relation),
// not a pack-declared edge, so it's a fixed set rather than legend-driven.
const CHAIN_RELS = new Set(["AMENDS", "SUPERSEDES", "NOVATES"]);

export type ViewMode = "full" | "lens";

function columnFor(label: string, mode: ViewMode = "full"): number {
  if (mode === "lens") {
    // Two columns now: the document, then the identity hubs its facts resolve
    // to. There used to be a third for the external customer/block register,
    // which was one deployment's master data and has been retired.
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


// ---------------------------------------------------------------------------
// ELK layered auto-layout — a progressive enhancement over the deterministic
// column stacking in buildGraph(). ELK assigns layers from the edge DAG (we
// pin each node to its semantic column via partitioning) and minimises edge
// crossings, so the Doc → Section → Fact → Hub flow stays legible at 700+
// nodes instead of degrading into a wall of overlapping rows. It runs async;
// the column layout shows instantly and ELK refines it a frame later. If ELK
// ever throws, we keep the column layout — nothing here can blank the canvas.
// ---------------------------------------------------------------------------

const elk = new ELK();

// Approximate rendered pill size; ELK only needs these to reserve space.
const NODE_W = 224;
const NODE_H = 58;

const ELK_OPTIONS: Record<string, string> = {
  "elk.algorithm": "layered",
  "elk.direction": "RIGHT",
  // Columns are the explanation — keep them wide apart and pinned by partition.
  "elk.partitioning.activate": "true",
  "elk.layered.spacing.nodeNodeBetweenLayers": "170",
  "elk.spacing.nodeNode": "26",
  "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
  "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
};

async function layoutElk(
  nodes: Node<ExploreNodeData>[],
  edges: Edge[],
  mode: ViewMode,
): Promise<Node<ExploreNodeData>[]> {
  const graph: ElkNode = {
    id: "root",
    layoutOptions: ELK_OPTIONS,
    children: nodes.map(n => ({
      id: n.id,
      width: NODE_W,
      height: NODE_H,
      // Pin to its semantic column so ELK never reshuffles Doc/Section/Fact/Hub
      // across layers — it only optimises ordering + crossings within them.
      layoutOptions: {
        "elk.partitioning.partition": String(columnFor(n.data.label, mode)),
      },
    })),
    edges: edges.map(e => ({ id: e.id, sources: [e.source], targets: [e.target] })),
  };
  const res = await elk.layout(graph);
  const pos = new Map((res.children ?? []).map(c => [c.id, c]));
  return nodes.map(n => {
    const p = pos.get(n.id);
    // Finite check: a NaN coordinate from a failed layout would poison the
    // fit-view transform and every SVG attribute downstream.
    return p && Number.isFinite(p.x) && Number.isFinite(p.y)
      ? { ...n, position: { x: p.x as number, y: p.y as number } }
      : n;
  });
}


// ---------------------------------------------------------------------------
// Family clustering — the "all documents" view spans every contract family
// (independent amendment chains, no edges between them beyond a shared
// identity hub). Laying them out as one global column stack piles every
// family's Doc → Section → Fact chain into the same few columns, so the
// canvas grows tall instead of wide and "fit to view" zooms out until
// nothing is readable — the exact "so small" symptom this was built to fix.
// Each family gets its own independent layered layout, then the families are
// arranged as a grid instead of one shared stack. A single family (the
// common case once the sidebar defaults to one) collapses to plain
// layoutElk() with zero grid overhead.
// ---------------------------------------------------------------------------

const FAMILY_GAP_X = 120;
const FAMILY_GAP_Y = 100;

/** Every node's family, propagated outward from each Document's own `group`
 * (or its own id, ungrouped = its own family — same rule the KB uses) along
 * whatever edges connect it. A shared identity hub reachable from two
 * families is claimed by whichever one is walked first — it has to live
 * somewhere for layout purposes, though the edge to the *other* family still
 * renders across the grid regardless of which cell owns the node. */
function computeFamilies(
  nodes: Node<ExploreNodeData>[],
  edges: Edge[],
): Map<string, string> {
  const adj = new Map<string, string[]>();
  const push = (a: string, b: string) => {
    const list = adj.get(a);
    if (list) list.push(b); else adj.set(a, [b]);
  };
  for (const e of edges) { push(e.source, e.target); push(e.target, e.source); }

  const family = new Map<string, string>();
  const docs = nodes
    .filter(n => n.data.label === "Document")
    .sort((a, b) => a.id.localeCompare(b.id)); // deterministic claim order

  for (const d of docs) {
    if (family.has(d.id)) continue;
    const key = (d.data.raw.props?.["group"] as string) || d.id;
    const queue = [d.id];
    family.set(d.id, key);
    while (queue.length) {
      const cur = queue.shift() as string;
      for (const nb of adj.get(cur) ?? []) {
        if (!family.has(nb)) { family.set(nb, key); queue.push(nb); }
      }
    }
  }
  // Anything an (unlikely) disconnected node leaves unclaimed gets its own
  // singleton family rather than being dropped.
  for (const n of nodes) if (!family.has(n.id)) family.set(n.id, n.id);
  return family;
}

function bboxOf(nodes: Node<ExploreNodeData>[]) {
  if (!nodes.length) return { minX: 0, minY: 0, w: NODE_W, h: NODE_H };
  const xs = nodes.map(n => n.position.x);
  const ys = nodes.map(n => n.position.y);
  const minX = Math.min(...xs);
  const minY = Math.min(...ys);
  return {
    minX, minY,
    w: Math.max(...xs) + NODE_W - minX,
    h: Math.max(...ys) + NODE_H - minY,
  };
}

async function layoutFamilyGrid(
  nodes: Node<ExploreNodeData>[],
  edges: Edge[],
  mode: ViewMode,
): Promise<Node<ExploreNodeData>[]> {
  const familyOf = computeFamilies(nodes, edges);
  const families = [...new Set(familyOf.values())].sort();
  if (families.length <= 1) return layoutElk(nodes, edges, mode);

  const byFamily = new Map<string, { nodes: Node<ExploreNodeData>[]; edges: Edge[] }>();
  for (const f of families) byFamily.set(f, { nodes: [], edges: [] });
  for (const n of nodes) byFamily.get(familyOf.get(n.id) as string)!.nodes.push(n);
  for (const e of edges) {
    const fs = familyOf.get(e.source);
    // Only an edge fully inside one family's subgraph is laid out with it —
    // a cross-family edge (e.g. a shared hub) would reference a node id ELK
    // never saw. It still renders afterwards; base.edges is untouched.
    if (fs && fs === familyOf.get(e.target)) byFamily.get(fs)!.edges.push(e);
  }

  const laidByFamily = await Promise.all(
    families.map(f => layoutElk(byFamily.get(f)!.nodes, byFamily.get(f)!.edges, mode)),
  );

  const cols = Math.max(1, Math.ceil(Math.sqrt(families.length)));
  const cellW = Math.max(...laidByFamily.map(ns => bboxOf(ns).w)) + FAMILY_GAP_X;
  const cellH = Math.max(...laidByFamily.map(ns => bboxOf(ns).h)) + FAMILY_GAP_Y;

  const out: Node<ExploreNodeData>[] = [];
  laidByFamily.forEach((ns, i) => {
    const box = bboxOf(ns);
    const dx = (i % cols) * cellW - box.minX;
    const dy = Math.floor(i / cols) * cellH - box.minY;
    for (const n of ns) out.push({ ...n, position: { x: n.position.x + dx, y: n.position.y + dy } });
  });
  return out;
}


interface ExploreNodeData {
  raw: GraphNode;
  label: string;
  title: string;
  subtitle?: string;
  group: string;
  proposal: boolean;
  selected?: boolean;
  /** Set only while a search is active: true = matches, false = doesn't
   * (dimmed). Undefined when no search is running — no highlight either way. */
  highlight?: boolean;
}


// ---------------------------------------------------------------------------
// Node renderer — pill whose colour family + glyph encodes the node group.
// ---------------------------------------------------------------------------

function GlyphNode({ data, selected }: NodeProps<ExploreNodeData>) {
  const klass = [
    "gnode",
    `gnode--g-${data.group}`,
    data.proposal && "gnode--proposal",
    selected && "gnode--selected",
    data.highlight === true && "gnode--match",
    data.highlight === false && "gnode--dim",
  ].filter(Boolean).join(" ");
  return (
    <div className={klass} title={data.title}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <span className="gnode__dot" style={{ background: groupColorVar(data.group) }} />
      <div className="gnode__body">
        <div className="gnode__label">
          {data.label}
          {data.proposal && <span className="gnode__badge">unverified</span>}
        </div>
        <div className="gnode__title">{data.title}</div>
      </div>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

const NODE_TYPES = { glyph: GlyphNode };


// ---------------------------------------------------------------------------
// Build react-flow nodes/edges from the payload + visibility set.
// ---------------------------------------------------------------------------

function shortTitle(n: GraphNode): string {
  const t = (n.title || n.label || "").replace(/\s+/g, " ").trim();
  return t.length > 46 ? t.slice(0, 45) + "…" : t;
}

function subtitleFor(n: GraphNode): string | undefined {
  const p = n.props || {};
  const page = p["page_no"];
  if (page !== undefined && page !== null) return `p${String(page)}`;
  const conf = p["confidence"];
  if (typeof conf === "number") return `conf ${conf}`;
  return undefined;
}


interface BuiltGraph {
  nodes: Node<ExploreNodeData>[];
  edges: Edge[];
}

/** Child id → parent id, from every edge of `edgeType` whose SOURCE carries
 * `parentLabel` (the overview payload's spine edges are always parent →
 * child: Agreement -[HAS_SECTION]-> Section, Section -[IN_SECTION]-> Fact). */
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

/**
 * Visibility model: only the Document/Agreement spine + identity hubs show by
 * default — a document family is a wall of clause structure otherwise, before
 * anyone has asked to see any of it. Sections show once their Agreement is in
 * `openAgreements`; facts show once their Section is in `openSections`. Two
 * clicks deep, same expand-on-click idea the code already uses one level
 * further in, just starting one level higher. Hubs/ext/proposals show
 * whenever an edge connects them to a visible node.
 */
function buildGraph(
  payload: GraphPayload,
  openSections: Set<string>,
  openAgreements: Set<string>,
  hiddenGroups: Set<string>,
  mode: ViewMode,
  matchIds: Set<string> = new Set(),
): BuiltGraph {
  const searching = matchIds.size > 0;
  const byId = new Map(payload.nodes.map(n => [n.id, n]));

  const agreementOfSection = parentMap(payload, "HAS_SECTION", "Agreement");
  const sectionOfFact = parentMap(payload, "IN_SECTION", "Section");
  const anyAgreementLink = agreementOfSection.size > 0;
  const anySectionLink = sectionOfFact.size > 0;

  const groupVisible = (n: GraphNode) =>
    !hiddenGroups.has(groupForLabel(n.label));

  const visible = new Set<string>();
  for (const n of payload.nodes) {
    if (!groupVisible(n)) continue;
    const col = columnFor(n.label, mode);
    if (col <= 1) { visible.add(n.id); continue; }        // Document/Agreement always
    if (col === 2) {                                       // Section
      const agr = agreementOfSection.get(n.id);
      // Show the section if: its agreement is open, OR it has no agreement,
      // OR there are no agreement links at all (a section-only slice).
      if (!anyAgreementLink || !agr || openAgreements.has(agr)) visible.add(n.id);
      continue;
    }
    if (isFactLabel(n.label)) {
      const sec = sectionOfFact.get(n.id);
      // Show the fact if: its section is open, OR it has no section, OR there
      // are no sections at all (so a fact-only slice still renders).
      if (!anySectionLink || !sec || openSections.has(sec)) visible.add(n.id);
      continue;
    }
    // Hubs / ext / proposals: defer; added below if linked to a visible node.
  }

  // Second pass — pull in hubs/ext/proposals attached to a visible node.
  for (const e of payload.edges) {
    const a = byId.get(e.source);
    const b = byId.get(e.target);
    if (!a || !b) continue;
    if (visible.has(e.source) && columnFor(b.label, mode) === 4 && groupVisible(b)) {
      visible.add(e.target);
    }
    if (visible.has(e.target) && columnFor(a.label, mode) === 4 && groupVisible(a)) {
      visible.add(e.source);
    }
  }

  // Deterministic LR layout — stack each column top-to-bottom.
  const colCursor: Record<number, number> = {};
  const nodes: Node<ExploreNodeData>[] = [];
  const ordered = [...visible]
    .map(id => byId.get(id)!)
    .sort((x, y) => columnFor(x.label, mode) - columnFor(y.label, mode)
      || x.label.localeCompare(y.label)
      || x.title.localeCompare(y.title));

  for (const n of ordered) {
    const col = columnFor(n.label, mode);
    const row = colCursor[col] = (colCursor[col] ?? 0) + 1;
    const group = groupForLabel(n.label);
    nodes.push({
      id: n.id,
      type: "glyph",
      position: { x: COL_X[col] ?? COL_X[COL_X.length - 1], y: row * ROW_H },
      data: {
        raw: n,
        label: n.label,
        title: shortTitle(n),
        subtitle: subtitleFor(n),
        group,
        proposal: isProposalNode(n),
        selected: false,   // applied as an overlay in <Inner>, post-layout
        highlight: searching ? matchIds.has(n.id) : undefined,
      },
      draggable: true,
      selectable: true,
    });
  }

  const edges: Edge[] = [];
  for (const e of payload.edges) {
    if (!visible.has(e.source) || !visible.has(e.target)) continue;
    const isIdentity = e.type === "RESOLVES_TO";
    // The declared document-to-document DAG (an amendment/novation/
    // supersedence) — a human statement, not a machine match, so it gets its
    // own look distinct from both RESOLVES_TO and plain structure.
    const isChain = CHAIN_RELS.has(e.type);
    edges.push({
      id: e.id,
      source: e.source,
      target: e.target,
      type: "smoothstep",
      label: e.type.replace(/_/g, " ").toLowerCase(),
      labelBgPadding: [4, 2],
      labelBgBorderRadius: 4,
      data: { raw: e },
      animated: false,
      className: isIdentity ? "gedge--identity" : isChain ? "gedge--chain" : "gedge--structural",
      style: {
        stroke: isIdentity ? "var(--teal)" : isChain ? "var(--warn)" : "rgba(255,255,255,0.18)",
        strokeWidth: isIdentity ? 1.6 : isChain ? 1.8 : 1.1,
        strokeDasharray: isIdentity ? "5 3" : undefined,
      },
    });
  }

  return { nodes, edges };
}


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

export function ExploreCanvas(props: ExploreCanvasProps) {
  return (
    <ReactFlowProvider>
      <Inner {...props} />
    </ReactFlowProvider>
  );
}

function Inner({
  payload, hiddenGroups, selectedNodeId, onSelectNode, onSelectEdge,
  mode = "full", search = "",
}: ExploreCanvasProps) {
  // Agreements/sections the user has opened. Both start closed: the default
  // view is just the document spine, not a clause structure nobody asked to
  // see yet. Click an Agreement to reveal its Sections, a Section to reveal
  // its Facts — same expand-on-click idea, two levels of it now.
  const [openAgreements, setOpenAgreements] = useState<Set<string>>(new Set());
  const [openSections, setOpenSections] = useState<Set<string>>(new Set());

  // Search — a plain substring match over the payload already in memory. A
  // match must force open whatever was hiding it (its Section, and that
  // Section's Agreement), or a hit just sits invisible behind two collapsed
  // levels.
  const needle = search.trim().toLowerCase();
  const matchIds = useMemo(() => {
    if (!needle) return new Set<string>();
    const m = new Set<string>();
    for (const n of payload.nodes) if (nodeSearchText(n).includes(needle)) m.add(n.id);
    return m;
  }, [payload, needle]);

  const forcedOpen = useMemo(() => {
    if (!matchIds.size) return { sections: new Set<string>(), agreements: new Set<string>() };
    const byId = new Map(payload.nodes.map(n => [n.id, n]));
    const agreementOfSection = parentMap(payload, "HAS_SECTION", "Agreement");
    const sectionOfFact = parentMap(payload, "IN_SECTION", "Section");
    const sections = new Set<string>();
    const agreements = new Set<string>();
    for (const id of matchIds) {
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
    return { sections, agreements };
  }, [matchIds, payload]);

  const effectiveOpenSections = useMemo(
    () => forcedOpen.sections.size
      ? new Set([...openSections, ...forcedOpen.sections])
      : openSections,
    [openSections, forcedOpen],
  );
  const effectiveOpenAgreements = useMemo(
    () => forcedOpen.agreements.size
      ? new Set([...openAgreements, ...forcedOpen.agreements])
      : openAgreements,
    [openAgreements, forcedOpen],
  );

  // Topology only — selection is NOT a dependency here, so toggling a
  // selection never triggers a re-layout (just a style overlay below).
  const base = useMemo(
    () => buildGraph(payload, effectiveOpenSections, effectiveOpenAgreements, hiddenGroups, mode, matchIds),
    [payload, effectiveOpenSections, effectiveOpenAgreements, hiddenGroups, mode, matchIds],
  );

  // Signature of the visible topology; layout re-runs only when this
  // changes — a search that doesn't open a new section (most keystrokes)
  // just restyles the existing layout instead of re-running it.
  const sig = useMemo(
    () => base.nodes.map(n => n.id).join("|") + "##"
        + base.edges.map(e => e.id).join("|"),
    [base],
  );

  // ELK-positioned nodes, tagged with the signature they were laid out for so
  // a slow layout from a previous topology can't be applied to a newer one.
  const [laid, setLaid] = useState<{ sig: string; nodes: Node<ExploreNodeData>[] }>(
    { sig: "", nodes: [] },
  );
  const layoutSeq = useRef(0);

  useEffect(() => {
    const seq = ++layoutSeq.current;
    if (base.nodes.length === 0) {
      setLaid({ sig, nodes: [] });
      return;
    }
    layoutFamilyGrid(base.nodes, base.edges, mode)
      .then(ns => { if (seq === layoutSeq.current) setLaid({ sig, nodes: ns }); })
      .catch(() => { if (seq === layoutSeq.current) setLaid({ sig, nodes: base.nodes }); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);

  // Use ELK/grid positions once they match the current topology; until then
  // fall back to buildGraph's instant column layout so the canvas is never
  // blank. Position comes from `laid` but DATA comes fresh from `base` —
  // base recomputes (e.g. new highlight flags from a search edit) far more
  // often than the layout re-runs, and a stale `laid.nodes.data` would
  // silently drop those restyles until the next real re-layout.
  const positioned = useMemo(() => {
    if (laid.sig !== sig) return base.nodes;
    const freshById = new Map(base.nodes.map(n => [n.id, n]));
    return laid.nodes.map(n => {
      const fresh = freshById.get(n.id);
      return fresh ? { ...n, data: fresh.data } : n;
    });
  }, [laid, sig, base]);

  // Selection overlay — cheap map, no re-layout. Drives both react-flow's
  // `selected` (the GlyphNode prop) and data.selected for styling.
  const nodes = useMemo(
    () => positioned.map(n => ({
      ...n,
      selected: n.id === selectedNodeId,
      data: { ...n.data, selected: n.id === selectedNodeId },
    })),
    [positioned, selectedNodeId],
  );
  const edges = base.edges;

  const { fitView } = useReactFlow();
  useEffect(() => {
    if (positioned.length === 0) return;
    const t = window.setTimeout(() => fitView({ padding: 0.18, duration: 240 }), 0);
    return () => window.clearTimeout(t);
  }, [laid.sig, positioned.length, fitView]);

  const toggle = (set: Dispatch<SetStateAction<Set<string>>>, id: string) =>
    set(curr => {
      const next = new Set(curr);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });

  const onNodeClick = (_: unknown, node: Node<ExploreNodeData>) => {
    onSelectEdge(null);
    onSelectNode(node.data.raw);
    // Clicking an Agreement toggles its sections open/closed; a Section
    // toggles its facts. Same expand-on-click gesture, one level apart.
    if (node.data.label === "Agreement") toggle(setOpenAgreements, node.id);
    else if (node.data.label === "Section") toggle(setOpenSections, node.id);
  };

  const onEdgeClick = (_: unknown, edge: Edge) => {
    const raw = (edge.data as { raw?: GraphEdge } | undefined)?.raw;
    if (raw) {
      onSelectNode(null);
      onSelectEdge(raw);
    }
  };

  if (payload.nodes.length === 0) {
    return <div className="rfgraph__empty">No graph data for this scope.</div>;
  }

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      onNodeClick={onNodeClick}
      onEdgeClick={onEdgeClick}
      onPaneClick={() => { onSelectNode(null); onSelectEdge(null); }}
      proOptions={{ hideAttribution: true }}
      panOnScroll
      zoomOnScroll
      minZoom={0.1}
      maxZoom={1.8}
      fitView
      fitViewOptions={{ padding: 0.18, duration: 240 }}
      nodesConnectable={false}
    >
      <Background gap={22} color="rgba(255,255,255,0.04)" />
      <Controls showInteractive={false} position="bottom-left" />
      <MiniMap
        pannable
        zoomable
        className="rfgraph__minimap"
        nodeColor={(n) =>
          groupColorVarRaw((n.data as ExploreNodeData)?.group || "other")
        }
      />
    </ReactFlow>
  );
}

// MiniMap can't read CSS vars, so resolve to concrete hexes.
function groupColorVarRaw(group: string): string {
  const map: Record<string, string> = {
    document: "#c4b5fd",
    agreement: "#a78bfa",
    section: "#76a3ff",
    fact: "#4dd4ac",
    identity: "#ec4899",
    evidence: "#f59e0b",
    proposal: "#f87171",
    other: "#6c7280",
  };
  return map[group] || map.other;
}
