/**
 * Layered, lazy-expanding graph canvas.
 *
 * The old radial / column layouts drowned the user in nodes — the cure
 * here is not a different layout, it's *fewer nodes at a time*:
 *
 *   1. Reshape the flat GraphPayload into a 5-layer tree:
 *
 *          Document  →  Section  →  Cluster  →  Fact  →  Evidence
 *
 *      Cluster is a synthetic per-label-per-section node ("Obligations
 *      · 12") so a single tile stands in for a dozen facts until the
 *      user opens it.
 *
 *   2. Render only what's "expanded". Document + Sections always show.
 *      Clusters appear under expanded Sections; Facts appear under
 *      expanded Clusters; Evidence appears under expanded Facts. The
 *      expansion set is local to this component, so the user's clicks
 *      drive what's visible — never the data shape.
 *
 *   3. Position nodes with a deterministic LR-walk: x = layer * stride,
 *      y groups children directly beside their parent so edges never
 *      cross. No layout engine, no rerender flicker.
 *
 * Used by both the per-answer subgraph (small slice, fully open by
 * default) and the doc-wide explorer (start closed, drill on click).
 */
import { useEffect, useMemo, useState } from "react";
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
import { GraphNode, GraphPayload } from "../types";
import { labelsInGroup, loadGraphLegend } from "./graphTheme";


// ---------------------------------------------------------------------------
// Internal tree shape
// ---------------------------------------------------------------------------


// Which labels are facts comes from the served legend (GET /graph/legend),
// built from the active schema. A literal list here treated this deployment's
// own fact types as "not a fact" and clustered them wrongly.
const isFactLabel = (label: string) => labelsInGroup("fact").has(label);


type NodeKind = "doc" | "section" | "cluster" | "fact" | "evidence";

interface CanvasNodeData {
  kind: NodeKind;
  label: string;          // raw label ("Obligation", "Section", "EvidenceSpan", "Cluster")
  title: string;          // display string
  subtitle?: string;      // optional under-title context
  count?: number;         // cluster: child count
  expanded?: boolean;
  focused?: boolean;
  selected?: boolean;
  raw?: GraphNode;
  hasChildren: boolean;
}


/** One row in the layered intermediate model. We flatten to react-flow's
 * Node[] only at render time (so layout stays a pure function of the
 * expansion set + the tree structure). */
interface LayerNode {
  id: string;
  parentId: string | null;
  layer: number;
  data: CanvasNodeData;
}


/** "DefinedTerm" -> "Defined terms". Derived from the label rather than looked
 * up in a table of one domain's words, so a new fact type reads correctly the
 * day it is declared. */
function pluralLabel(label: string): string {
  const spaced = label.replace(/([a-z0-9])([A-Z])/g, "$1 $2");
  const head = spaced.charAt(0).toUpperCase() + spaced.slice(1).toLowerCase();
  if (/(s|x|z|ch|sh)$/i.test(head)) return `${head}es`;
  if (/[^aeiou]y$/i.test(head)) return `${head.slice(0, -1)}ies`;
  return `${head}s`;
}


function cmpSection(a: string, b: string): number {
  const parts = (s: string) => s.split(/[.\-(]/).map(p => {
    const n = parseInt(p, 10);
    return Number.isNaN(n) ? p : n;
  });
  const aa = parts(a);
  const bb = parts(b);
  const len = Math.max(aa.length, bb.length);
  for (let i = 0; i < len; i++) {
    const x = aa[i];
    const y = bb[i];
    if (x === undefined) return -1;
    if (y === undefined) return 1;
    if (typeof x === "number" && typeof y === "number") {
      if (x !== y) return x - y;
    } else if (String(x) !== String(y)) {
      return String(x).localeCompare(String(y));
    }
  }
  return 0;
}


function sectionDisplay(sec: GraphNode): string {
  const num = sec.props["section_num"] as string | undefined;
  const title = sec.props["title"] as string | undefined;
  if (num && title) return `§${num} — ${title}`;
  if (num) return `§${num}`;
  return title || sec.title || sec.id;
}


function snippet(raw: GraphNode, max = 80): string {
  const text = (raw.props["text_span"] as string) || raw.title || "";
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > max ? clean.slice(0, max - 1) + "…" : clean;
}


/**
 * Walk the flat GraphPayload and produce the full layered model: every
 * possible node the user could see, regardless of expansion. The caller
 * passes the expansion set at render time to filter down to visible.
 *
 * Returns `null` if the payload is empty (caller renders a placeholder).
 */
function buildLayers(payload: GraphPayload): {
  nodes: LayerNode[];
  childrenOf: Map<string, string[]>;
} | null {
  if (payload.nodes.length === 0) return null;

  const byId = new Map(payload.nodes.map(n => [n.id, n]));
  const outEdges = new Map<string, { type: string; target: string }[]>();
  for (const e of payload.edges) {
    if (!outEdges.has(e.source)) outEdges.set(e.source, []);
    outEdges.get(e.source)!.push({ type: e.type, target: e.target });
  }

  // EvidenceSpan → Section (via IN_SECTION)
  const sectionForEv = new Map<string, string>();
  for (const [src, edges] of outEdges) {
    const n = byId.get(src);
    if (n?.label !== "EvidenceSpan") continue;
    const inSec = edges.find(e => e.type === "IN_SECTION");
    if (inSec) sectionForEv.set(src, inSec.target);
  }

  // Fact → its EvidenceSpan ids
  const evForFact = new Map<string, string[]>();
  for (const n of payload.nodes) {
    if (!isFactLabel(n.label)) continue;
    evForFact.set(
      n.id,
      (outEdges.get(n.id) || [])
        .filter(e => e.type === "SUPPORTED_BY")
        .map(e => e.target),
    );
  }

  // Fact → primary section (first evidence's section)
  const sectionForFact = new Map<string, string>();
  for (const [fid, evs] of evForFact) {
    for (const ev of evs) {
      const sec = sectionForEv.get(ev);
      if (sec) { sectionForFact.set(fid, sec); break; }
    }
  }

  const doc = payload.nodes.find(n => n.label === "Document" || n.label === "Agreement");
  if (!doc) return null;

  const sections = payload.nodes.filter(n => n.label === "Section").sort(
    (a, b) => cmpSection(
      (a.props["section_num"] as string) || "",
      (b.props["section_num"] as string) || "",
    ),
  );

  const layerNodes: LayerNode[] = [];
  const childrenOf = new Map<string, string[]>();
  const addChild = (parentId: string, childId: string) => {
    if (!childrenOf.has(parentId)) childrenOf.set(parentId, []);
    childrenOf.get(parentId)!.push(childId);
  };

  // Layer 0 — Document
  const docId = doc.id;
  layerNodes.push({
    id: docId,
    parentId: null,
    layer: 0,
    data: {
      kind: "doc",
      label: doc.label,
      title: (doc.props["title"] as string) || doc.title || "Document",
      raw: doc,
      hasChildren: sections.length > 0,
    },
  });

  // Layer 1 — Sections (only ones with at least one fact, plus a synthetic
  // "Document-level" section for parties / dates on the cover page)
  const factsPerSection = new Map<string, GraphNode[]>();
  for (const [fid, sid] of sectionForFact) {
    const f = byId.get(fid);
    if (!f) continue;
    if (!factsPerSection.has(sid)) factsPerSection.set(sid, []);
    factsPerSection.get(sid)!.push(f);
  }
  const orphans = [...evForFact.keys()]
    .filter(fid => !sectionForFact.has(fid))
    .map(fid => byId.get(fid))
    .filter((n): n is GraphNode => !!n);

  const visibleSections = sections.filter(s => factsPerSection.get(s.id)?.length);

  // Add the "Document-level" pseudo-section for orphans (if any)
  if (orphans.length > 0) {
    const id = "__unsectioned";
    layerNodes.push({
      id,
      parentId: docId,
      layer: 1,
      data: {
        kind: "section",
        label: "Section",
        title: "Document level",
        count: orphans.length,
        hasChildren: true,
      },
    });
    addChild(docId, id);
    factsPerSection.set(id, orphans);
  }
  for (const s of visibleSections) {
    layerNodes.push({
      id: s.id,
      parentId: docId,
      layer: 1,
      data: {
        kind: "section",
        label: "Section",
        title: sectionDisplay(s),
        count: factsPerSection.get(s.id)?.length || 0,
        raw: s,
        hasChildren: true,
      },
    });
    addChild(docId, s.id);
  }

  // Layer 2 — Clusters (one per label per section)
  // Layer 3 — Facts
  // Layer 4 — Evidence
  for (const [sid, facts] of factsPerSection) {
    const byLabel: Record<string, GraphNode[]> = {};
    for (const f of facts) (byLabel[f.label] = byLabel[f.label] || []).push(f);

    for (const [lbl, group] of Object.entries(byLabel).sort(([a], [b]) => a.localeCompare(b))) {
      const clusterId = `${sid}::cluster::${lbl}`;
      layerNodes.push({
        id: clusterId,
        parentId: sid,
        layer: 2,
        data: {
          kind: "cluster",
          label: lbl,
          title: pluralLabel(lbl),
          count: group.length,
          hasChildren: true,
        },
      });
      addChild(sid, clusterId);

      for (const f of group.sort((a, b) => a.title.localeCompare(b.title))) {
        const evs = (evForFact.get(f.id) || [])
          .map(eid => byId.get(eid))
          .filter((n): n is GraphNode => !!n)
          .sort(
            (a, b) =>
              Number(a.props["page_no"] || 0) - Number(b.props["page_no"] || 0),
          );
        layerNodes.push({
          id: f.id,
          parentId: clusterId,
          layer: 3,
          data: {
            kind: "fact",
            label: f.label,
            title: f.title,
            raw: f,
            hasChildren: evs.length > 0,
          },
        });
        addChild(clusterId, f.id);

        for (const e of evs) {
          layerNodes.push({
            id: e.id,
            parentId: f.id,
            layer: 4,
            data: {
              kind: "evidence",
              label: "EvidenceSpan",
              title: snippet(e),
              subtitle: `p${e.props["page_no"] || "?"}`,
              raw: e,
              hasChildren: false,
            },
          });
          addChild(f.id, e.id);
        }
      }
    }
  }

  return { nodes: layerNodes, childrenOf };
}


// ---------------------------------------------------------------------------
// Layout — deterministic LR walk
// ---------------------------------------------------------------------------


const LAYER_STRIDE_X = 240;
const ROW_HEIGHT     = 64;
const ROW_GAP        = 12;


/**
 * Compute (x, y) for every visible node so that:
 *   - column = layer * LAYER_STRIDE_X
 *   - within a column, siblings group directly under their parent's centre
 *
 * Visibility rules:
 *   layer 0 (Document)   : always
 *   layer 1 (Section)    : always
 *   layer 2 (Cluster)    : parent Section is expanded
 *   layer 3 (Fact)       : parent Cluster is expanded
 *   layer 4 (Evidence)   : parent Fact is expanded
 */
function layoutLayers(
  layered: { nodes: LayerNode[]; childrenOf: Map<string, string[]> },
  expanded: Set<string>,
): { nodes: Node<CanvasNodeData>[]; edges: Edge[] } {
  const byId = new Map(layered.nodes.map(n => [n.id, n]));
  const visible = new Set<string>();
  const root = layered.nodes.find(n => n.layer === 0);
  if (!root) return { nodes: [], edges: [] };
  const walk = (id: string) => {
    visible.add(id);
    const node = byId.get(id);
    if (!node) return;
    const isExpanded = expanded.has(id) || node.layer < 1; // doc always open
    if (!isExpanded) return;
    for (const childId of layered.childrenOf.get(id) || []) {
      walk(childId);
    }
  };
  walk(root.id);

  // Group visible nodes by layer.
  const byLayer: Record<number, LayerNode[]> = {};
  for (const id of visible) {
    const n = byId.get(id);
    if (!n) continue;
    (byLayer[n.layer] = byLayer[n.layer] || []).push(n);
  }

  // Y assignment is recursive top-down: visit parents in their column,
  // place each child stack centred on its parent.
  const y = new Map<string, number>();
  let cursor = 0;
  const placeSubtree = (id: string): { top: number; height: number } => {
    const kids = (layered.childrenOf.get(id) || []).filter(c => visible.has(c));
    if (kids.length === 0) {
      const top = cursor * (ROW_HEIGHT + ROW_GAP);
      y.set(id, top);
      cursor++;
      return { top, height: ROW_HEIGHT };
    }
    const childRanges = kids.map(c => placeSubtree(c));
    const top = childRanges[0].top;
    const last = childRanges[childRanges.length - 1];
    const bottom = last.top + last.height;
    // Centre parent vertically over its children
    const parentY = (top + bottom - ROW_HEIGHT) / 2;
    y.set(id, parentY);
    return { top: Math.min(parentY, top), height: bottom - Math.min(parentY, top) };
  };
  placeSubtree(root.id);

  const nodes: Node<CanvasNodeData>[] = [];
  for (const id of visible) {
    const n = byId.get(id);
    if (!n) continue;
    nodes.push({
      id,
      type: "kbnode",
      data: { ...n.data, expanded: expanded.has(id) },
      position: { x: n.layer * LAYER_STRIDE_X, y: y.get(id) || 0 },
      draggable: false,
      selectable: true,
    });
  }

  // Edges — only between visible parent/child pairs.
  const edges: Edge[] = [];
  for (const [pid, children] of layered.childrenOf) {
    if (!visible.has(pid)) continue;
    for (const cid of children) {
      if (!visible.has(cid)) continue;
      edges.push({
        id: `${pid}__${cid}`,
        source: pid,
        target: cid,
        type: "smoothstep",
        style: { stroke: "rgba(255, 255, 255, 0.18)", strokeWidth: 1.1 },
      });
    }
  }
  return { nodes, edges };
}


// ---------------------------------------------------------------------------
// Node component
// ---------------------------------------------------------------------------


function KbNode({ data, selected }: NodeProps<CanvasNodeData>) {
  const klass = [
    "rfnode",
    `rfnode--${data.kind}`,
    `rfnode--lab-${data.label}`,
    selected && "rfnode--selected",
    data.focused && "rfnode--focused",
    data.expanded && data.hasChildren && "rfnode--open",
  ].filter(Boolean).join(" ");
  return (
    <div className={klass}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div className="rfnode__row">
        <span className="rfnode__label">{data.label}</span>
        {typeof data.count === "number" && (
          <span className="rfnode__count">{data.count}</span>
        )}
      </div>
      <div className="rfnode__title">{data.title}</div>
      {data.subtitle && <div className="rfnode__subtitle">{data.subtitle}</div>}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

const NODE_TYPES = { kbnode: KbNode };


// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------


export interface GraphCanvasProps {
  payload: GraphPayload;
  /** "inline" = compact, open-by-default. "explore" = big, closed-by-default. */
  mode: "inline" | "explore";
  /** If set, this leaf evidence node is highlighted (citation focus). */
  focusedEvidenceId?: string | null;
  /** Fired when the user clicks a non-toggleable leaf (typically evidence). */
  onLeafClick?: (raw: GraphNode) => void;
  /** Fired when any node is selected (for the right-side detail panel). */
  onSelect?: (raw: GraphNode | null) => void;
  /** Show the mini-map (only relevant for `mode="explore"`). */
  showMiniMap?: boolean;
}


export function GraphCanvas(props: GraphCanvasProps) {
  // The legend decides which labels are facts. It arrives from the API, so
  // make sure the fetch is in flight: this tree can be the first thing on
  // screen, under the very first answer.
  const [ready, setReady] = useState(false);
  useEffect(() => { loadGraphLegend().then(() => setReady(true)); }, []);
  return (
    <ReactFlowProvider>
      <Inner key={ready ? "legend" : "pending"} {...props} />
    </ReactFlowProvider>
  );
}


function Inner({
  payload, mode, focusedEvidenceId, onLeafClick, onSelect, showMiniMap = false,
}: GraphCanvasProps) {
  const layered = useMemo(() => buildLayers(payload), [payload]);

  // For inline mode, start fully expanded (the slice is already small).
  // For explore mode, only show layer 0/1 — let the user drill.
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    if (!layered) {
      setExpanded(new Set());
      return;
    }
    const next = new Set<string>();
    if (mode === "inline") {
      for (const n of layered.nodes) next.add(n.id);
    } else {
      // Open the doc + the first section by default so it isn't empty.
      const doc = layered.nodes.find(n => n.layer === 0);
      const firstSection = layered.nodes.find(n => n.layer === 1);
      if (doc) next.add(doc.id);
      if (firstSection) next.add(firstSection.id);
    }
    setExpanded(next);
  }, [layered, mode]);

  const { nodes, edges } = useMemo(() => {
    if (!layered) return { nodes: [], edges: [] };
    return layoutLayers(layered, expanded);
  }, [layered, expanded]);

  // Re-fit the viewport whenever the visible node set changes — inline
  // graphs grow horizontally as users expand, so without this they have
  // to manually pan/zoom to find new nodes.
  const { fitView } = useReactFlow();
  useEffect(() => {
    if (nodes.length === 0) return;
    const t = window.setTimeout(
      () => fitView({ padding: 0.2, duration: 240 }),
      0,
    );
    return () => window.clearTimeout(t);
  }, [nodes, fitView]);

  // Decorate nodes with focused / selected flags right before render.
  const decoratedNodes = useMemo(
    () =>
      nodes.map(n => ({
        ...n,
        data: { ...n.data, focused: n.id === focusedEvidenceId },
      })),
    [nodes, focusedEvidenceId],
  );

  const handleClick = (_: unknown, node: Node<CanvasNodeData>) => {
    // Leaf (evidence): fire the citation callback; don't toggle.
    if (node.data.kind === "evidence") {
      if (node.data.raw) {
        onLeafClick?.(node.data.raw);
        onSelect?.(node.data.raw);
      }
      return;
    }
    // Anything with children → toggle expansion AND select for detail panel.
    if (node.data.hasChildren) {
      setExpanded(curr => {
        const next = new Set(curr);
        if (next.has(node.id)) next.delete(node.id);
        else next.add(node.id);
        return next;
      });
    }
    if (node.data.raw) onSelect?.(node.data.raw);
    else onSelect?.(null);
  };

  if (!layered) {
    return <div className="rfgraph__empty">No graph data.</div>;
  }

  return (
    <ReactFlow
      nodes={decoratedNodes}
      edges={edges}
      nodeTypes={NODE_TYPES}
      onNodeClick={handleClick}
      proOptions={{ hideAttribution: true }}
      panOnScroll
      zoomOnScroll
      minZoom={0.15}
      maxZoom={1.6}
      defaultEdgeOptions={{ animated: false }}
      fitView
      fitViewOptions={{ padding: 0.2, duration: 240 }}
      nodesDraggable={false}
      nodesConnectable={false}
    >
      <Background gap={20} color="rgba(255,255,255,0.04)" />
      <Controls
        showInteractive={false}
        position={mode === "inline" ? "bottom-right" : "bottom-left"}
      />
      {mode === "explore" && showMiniMap && (
        <MiniMap pannable zoomable className="rfgraph__minimap" />
      )}
    </ReactFlow>
  );
}
