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
 *     fact → hub) plus an expand-on-click model: only the document spine
 *     and the facts of the *focused* section render until the user drills in.
 *   • Every node and edge comes from the store. Nothing is invented here.
 *
 * Selection (node or edge) is lifted to the parent so the right-hand
 * explainability panel can show properties + provenance and offer a
 * "view in PDF" jump that reuses the existing citation/bbox machinery.
 */
import { useEffect, useMemo, useRef, useState } from "react";
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
} from "./graphTheme";


// ---------------------------------------------------------------------------
// Layering — assign every node a column so the layout reads left → right:
//   0 Document · 1 Agreement · 2 Section · 3 Fact · 4 hub / evidence / proposal
// ---------------------------------------------------------------------------

// Read at call time, not at module load: the legend arrives from the API.
const isFactLabel = (label: string) => labelsInGroup("fact").has(label);

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


interface ExploreNodeData {
  raw: GraphNode;
  label: string;
  title: string;
  subtitle?: string;
  group: string;
  proposal: boolean;
  selected?: boolean;
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

/**
 * Visibility model: the document spine (Document/Agreement/Section) + identity
 * hubs always show. Facts show when their section is in `openSections`, or
 * when nothing is openable (no sections — e.g. a fact-only slice) so the view
 * is never empty. Hubs/ext/proposals show whenever an edge connects them to a
 * visible node.
 */
function buildGraph(
  payload: GraphPayload,
  openSections: Set<string>,
  hiddenGroups: Set<string>,
  mode: ViewMode,
): BuiltGraph {
  const byId = new Map(payload.nodes.map(n => [n.id, n]));

  // Section → its facts (via IN_SECTION). Fact → its section.
  const sectionOfFact = new Map<string, string>();
  for (const e of payload.edges) {
    if (e.type === "IN_SECTION") {
      // IN_SECTION here is Section -> Fact (overview) — source is the section.
      const src = byId.get(e.source);
      const tgt = byId.get(e.target);
      if (src?.label === "Section" && tgt) sectionOfFact.set(e.target, e.source);
    }
  }

  const anySectionLink = sectionOfFact.size > 0;

  const groupVisible = (n: GraphNode) =>
    !hiddenGroups.has(groupForLabel(n.label));

  const visible = new Set<string>();
  for (const n of payload.nodes) {
    if (!groupVisible(n)) continue;
    const col = columnFor(n.label, mode);
    if (col <= 2) { visible.add(n.id); continue; }        // spine always
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
      },
      draggable: true,
      selectable: true,
    });
  }

  const edges: Edge[] = [];
  for (const e of payload.edges) {
    if (!visible.has(e.source) || !visible.has(e.target)) continue;
    const isIdentity = e.type === "RESOLVES_TO";
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
      className: isIdentity ? "gedge--identity" : "gedge--structural",
      style: {
        stroke: isIdentity ? "var(--teal)" : "rgba(255,255,255,0.18)",
        strokeWidth: isIdentity ? 1.6 : 1.1,
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
  mode = "full",
}: ExploreCanvasProps) {
  // Sections the user has opened. Default: open the first section so the
  // canvas shows real facts immediately instead of a bare spine.
  const [openSections, setOpenSections] = useState<Set<string>>(new Set());

  const sectionIds = useMemo(
    () => payload.nodes.filter(n => n.label === "Section").map(n => n.id),
    [payload],
  );

  useEffect(() => {
    setOpenSections(new Set(sectionIds.slice(0, 1)));
  }, [sectionIds.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  // Topology only — selection is NOT a dependency here, so toggling a
  // selection never triggers a re-layout (just a style overlay below).
  const base = useMemo(
    () => buildGraph(payload, openSections, hiddenGroups, mode),
    [payload, openSections, hiddenGroups, mode],
  );

  // Signature of the visible topology; ELK re-runs only when this changes.
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
    layoutElk(base.nodes, base.edges, mode)
      .then(ns => { if (seq === layoutSeq.current) setLaid({ sig, nodes: ns }); })
      .catch(() => { if (seq === layoutSeq.current) setLaid({ sig, nodes: base.nodes }); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);

  // Use ELK positions once they match the current topology; until then fall
  // back to buildGraph's instant column layout so the canvas is never blank.
  const positioned = laid.sig === sig ? laid.nodes : base.nodes;

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

  const onNodeClick = (_: unknown, node: Node<ExploreNodeData>) => {
    onSelectEdge(null);
    onSelectNode(node.data.raw);
    // Clicking a Section toggles its facts open/closed.
    if (node.data.label === "Section") {
      setOpenSections(curr => {
        const next = new Set(curr);
        if (next.has(node.id)) next.delete(node.id);
        else next.add(node.id);
        return next;
      });
    }
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
