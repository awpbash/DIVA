/**
 * Full knowledge-graph explorer tab.
 *
 *   ┌──── filters ────┐  ┌──── canvas (ExploreCanvas) ──┐  ┌── inspect ──┐
 *   │ search…         │  │  Doc → Agreement → Section → │  │ props +     │
 *   │ category chips  │  │  Fact → Hub/Ext/Proposal     │  │ provenance  │
 *   │ group toggles   │  │  (spine only — click to open)│  │ + PDF jump  │
 *   │ legend          │  │                              │  │             │
 *   └─────────────────┘  └──────────────────────────────┘  └─────────────┘
 *
 * Works at *any* scope: a single contract (doc picked in the sidebar) or
 * ALL documents (the default) — the cross-document identity hubs are the
 * whole reason the all-docs view matters. Everything rendered comes from
 * /graph/overview; the inspector reuses the citation/evidence machinery so a
 * fact can be traced to its page + bbox highlight in the source PDF.
 */
import { useEffect, useMemo, useState } from "react";
import { getHubGraph, getOverviewGraph } from "../api";
import { DocumentMeta, GraphEdge, GraphNode, GraphPayload } from "../types";
import { ExploreCanvas, ViewMode } from "./ExploreCanvas";
import { GraphInspector } from "./GraphInspector";
import {
  groupColorVar, groupForLabel, labelsInGroup, nodeSearchText, useGraphLegend,
} from "./graphTheme";
import { IconClose, IconPanelRight } from "./Icon";


// The fact labels the extractor emits, for the category filter chips. Comes
// from the served legend, so the chips are this deployment's own fact types
// rather than a list copied from whichever domain was built first.

const DEFAULT_NODE_CAP = 220;

// Sentinel for the explicit "every document" choice in the family picker,
// distinct from `null` (families not loaded yet).
const ALL_FAMILIES = "__all__";

// Stale-while-revalidate cache so tab-switches don't refetch.
const OVERVIEW_CACHE = new Map<string, GraphPayload>();
const cacheKey = (docId: string | null, group: string | undefined, labels: string[], limit: number) =>
  `${docId ?? group ?? "__all"}::${[...labels].sort().join(",")}::${limit}`;


interface Props {
  doc: DocumentMeta | null;
  /** Every document, for the contract-family picker (ignored once `doc`
   * scopes to a single contract). */
  docs: DocumentMeta[];
  /** Jump to a citation in the PDF panel — reuses the chat citation flow. */
  onViewEvidence?: (evidenceId: string, docId: string) => void;
}


export function GraphExplorer({ doc, docs, onViewEvidence }: Props) {
  const legend = useGraphLegend();
  const factLabels = useMemo(
    () => [...labelsInGroup("fact")].filter(l => l !== "Fact").sort(),
    [legend]);
  const [labels, setLabels] = useState<Set<string>>(new Set());
  // Select every fact category once the legend lands. Empty means "everything"
  // to the API, so the first fetch is correct either way.
  useEffect(() => { setLabels(new Set(factLabels)); }, [factLabels]);
  const [hiddenGroups, setHiddenGroups] = useState<Set<string>>(new Set());
  const [payload, setPayload] = useState<GraphPayload>({ nodes: [], edges: [] });
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<GraphEdge | null>(null);
  const [limit, setLimit] = useState(DEFAULT_NODE_CAP);
  const [loading, setLoading] = useState(false);
  const [focusMode, setFocusMode] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  // "full" = the whole graph (expand-on-click); "lens" = the cross-doc
  // identity story only.
  const [viewMode, setViewMode] = useState<ViewMode>("full");

  // Clear the payload in the SAME render as the mode flip. Otherwise one frame
  // renders the old graph re-columned for the new mode, and the fit-view
  // animation kicked off in that hybrid frame can tween through a zero-size
  // view and write NaN into the canvas transform (console SVG errors).
  const switchMode = (m: ViewMode) => {
    if (m === viewMode) return;
    setViewMode(m);
    setPayload({ nodes: [], edges: [] });
  };

  const docId = doc?.doc_id ?? null;

  // Contract families, derived from the document list — same grouping rule
  // the KB uses (a shared `group` is one amendment chain; ungrouped = its
  // own family). Landing on "every document" merged every unrelated chain
  // into one shared layout by default, which is what made the graph look
  // like a wall of nodes meaning nothing — so the default here is ONE
  // family, exactly like the Knowledge tab already defaults to `f[0]`.
  const families = useMemo(() => {
    const map = new Map<string, { key: string; name: string; count: number }>();
    for (const d of docs) {
      const key = d.group || d.doc_id;
      const name = d.group_name || d.group || d.title;
      const entry = map.get(key) ?? { key, name, count: 0 };
      entry.count += 1;
      map.set(key, entry);
    }
    return [...map.values()].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  }, [docs]);

  const [familyKey, setFamilyKey] = useState<string | null>(null);
  useEffect(() => {
    if (doc) return; // an explicit single-contract scope always wins
    setFamilyKey(prev => (prev && families.some(f => f.key === prev)) ? prev : (families[0]?.key ?? null));
  }, [families, doc]);

  const groupParam = !doc && familyKey && familyKey !== ALL_FAMILIES ? familyKey : undefined;

  useEffect(() => {
    const labelList = [...labels];
    const key = `${viewMode}::${cacheKey(docId, groupParam, labelList, limit)}`;
    const cached = OVERVIEW_CACHE.get(key);
    if (cached) {
      setPayload(cached);
      setLoading(false);
      return;
    }
    setLoading(true);
    // Clear first so a lens layout never paints over stale full-graph nodes.
    setPayload({ nodes: [], edges: [] });
    let cancelled = false;
    // docId and groupParam both null => every document (cross-doc) — the
    // explicit "All documents" choice, or the fallback before families load.
    const fetcher = viewMode === "lens"
      ? getHubGraph(docId ?? undefined, groupParam)
      : getOverviewGraph({ docId: docId ?? undefined, group: groupParam, labels: labelList, limit });
    fetcher
      .then(p => {
        OVERVIEW_CACHE.set(key, p);
        if (cancelled) return;
        setPayload(p);
        setSelectedNode(s => (s && p.nodes.find(n => n.id === s.id)) ? s : null);
        setSelectedEdge(null);
      })
      .catch(err => console.error("graph:", err))
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId, groupParam, labels, limit, viewMode]);

  // Esc exits focus mode.
  useEffect(() => {
    if (!focusMode) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setFocusMode(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focusMode]);

  // Live group counts for the legend / filter affordance.
  const groupCounts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const n of payload.nodes) {
      const g = groupForLabel(n.label);
      c[g] = (c[g] || 0) + 1;
    }
    return c;
  }, [payload]);

  const searchNeedle = searchQuery.trim().toLowerCase();
  const matchCount = useMemo(() => {
    if (!searchNeedle) return 0;
    return payload.nodes.filter(n => nodeSearchText(n).includes(searchNeedle)).length;
  }, [payload, searchNeedle]);

  const scopeName = doc
    ? doc.title
    : familyKey && familyKey !== ALL_FAMILIES
      ? (families.find(f => f.key === familyKey)?.name ?? "family")
      : "all documents";

  const hasSelection = !!selectedNode || !!selectedEdge;
  const showDetail = !focusMode && hasSelection;
  const showSidebar = !focusMode;

  const toggleLabel = (lbl: string) =>
    setLabels(curr => {
      const next = new Set(curr);
      if (next.has(lbl)) next.delete(lbl);
      else next.add(lbl);
      return next;
    });

  const toggleGroup = (g: string) =>
    setHiddenGroups(curr => {
      const next = new Set(curr);
      if (next.has(g)) next.delete(g);
      else next.add(g);
      return next;
    });

  return (
    <div className="pane" style={{ minHeight: 0 }}>
      <div className="pane__header">
        <div className="pane__title">Knowledge graph</div>
        <div className="pane__subtitle">
          · {scopeName} · {payload.nodes.length} nodes / {payload.edges.length} edges
        </div>
        {loading && <div className="pane__loading">loading…</div>}
        <div style={{ flex: 1 }} />
        <div className="graph-mode" role="tablist" aria-label="Graph view">
          <button
            type="button"
            role="tab"
            aria-selected={viewMode === "full"}
            className={`graph-mode__btn${viewMode === "full" ? " graph-mode__btn--on" : ""}`}
            onClick={() => switchMode("full")}
          >
            Full graph
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={viewMode === "lens"}
            className={`graph-mode__btn${viewMode === "lens" ? " graph-mode__btn--on" : ""}`}
            onClick={() => switchMode("lens")}
            title="Show the parties named in documents and the roles that connect them"
          >
            Entity map
          </button>
        </div>
        <button
          type="button"
          className="graph-header-action"
          onClick={() => setFocusMode(f => !f)}
          title={focusMode ? "Show panels (Esc)" : "Maximise canvas"}
          aria-label={focusMode ? "Show panels" : "Maximise canvas"}
        >
          {focusMode ? <IconClose size={14} /> : <IconPanelRight size={14} />}
          <span>{focusMode ? "Show panels" : "Focus canvas"}</span>
        </button>
      </div>

      <div
        className="explorer"
        style={{
          gridTemplateColumns: `${showSidebar ? "240px " : ""}1fr`,
        }}
      >
        {showSidebar && (
          <aside className="explorer__sidebar">
            <div className="explorer__filter-group">
              <h4>Search</h4>
              <input
                type="text"
                className="explorer__search"
                placeholder="Find a node by name or value…"
                value={searchQuery}
                onChange={e => setSearchQuery(e.target.value)}
              />
              {searchQuery && (
                <div className="explorer__range-readout">
                  {matchCount} match{matchCount === 1 ? "" : "es"}
                </div>
              )}
            </div>

            {!doc && families.length > 1 && (
              <div className="explorer__filter-group">
                <h4>Contract family</h4>
                <select
                  className="explorer__select"
                  value={familyKey ?? ALL_FAMILIES}
                  onChange={e => setFamilyKey(e.target.value)}
                >
                  {families.map(f => (
                    <option key={f.key} value={f.key}>{f.name} ({f.count})</option>
                  ))}
                  <option value={ALL_FAMILIES}>All documents ({docs.length})</option>
                </select>
              </div>
            )}

            <div className="explorer__filter-group">
              <h4>Legend</h4>
              <div className="legend">
                {legend.groups.map(l => {
                  const off = hiddenGroups.has(l.group);
                  const count = groupCounts[l.group] || 0;
                  return (
                    <button
                      key={l.group}
                      type="button"
                      className={`legend__row ${off ? "legend__row--off" : ""}`}
                      onClick={() => toggleGroup(l.group)}
                      title={`${l.hint} — click to ${off ? "show" : "hide"}`}
                    >
                      <span
                        className="legend__swatch"
                        style={{ background: groupColorVar(l.group) }}
                      />
                      <span className="legend__label">{l.label}</span>
                      <span className="legend__count">{count}</span>
                    </button>
                  );
                })}
              </div>
            </div>

            {viewMode === "full" && (
              <>
                <div className="explorer__filter-group">
                  <h4>Facts to include</h4>
                  <div className="explorer__chips">
                    {factLabels.map(lbl => {
                      const on = labels.has(lbl);
                      return (
                        <button
                          key={lbl}
                          type="button"
                          className={`chip ${on ? "chip--on" : ""}`}
                          onClick={() => toggleLabel(lbl)}
                        >
                          {lbl}
                        </button>
                      );
                    })}
                  </div>
                </div>

                <div className="explorer__filter-group">
                  <h4>Fact limit</h4>
                  <input
                    type="range"
                    min={40}
                    max={400}
                    step={20}
                    value={limit}
                    onChange={e => setLimit(Number(e.target.value))}
                    className="explorer__range"
                  />
                  <div className="explorer__range-readout">Load up to {limit} facts</div>
                </div>
              </>
            )}

            {viewMode === "lens" && (
              <div className="explorer__filter-group explorer__hint">
                <h4>Entity map</h4>
                <p className="explorer__note">
                  Shows the parties named in each document and the roles they
                  play. Shared party nodes make cross-document connections
                  visible without mixing in clause structure.
                </p>
              </div>
            )}

            <div className="explorer__filter-group explorer__hint">
              <h4>Tips</h4>
              <ul>
                {viewMode === "full" ? (
                  <>
                    <li>Click an agreement to reveal its sections.</li>
                    <li>Click a section to reveal its facts.</li>
                    <li>Click a node to inspect it and trace it to its source.</li>
                    <li>Click a dashed edge to see why the link was made.</li>
                  </>
                ) : (
                  <>
                    <li>Each arrow is a document → party relationship.</li>
                    <li>Click a party to spotlight every connected document.</li>
                    <li>Click an arrow to see its role and supporting knowledge fields.</li>
                  </>
                )}
                <li>Scroll to zoom, drag to pan.</li>
              </ul>
            </div>
          </aside>
        )}

        <main className="explorer__main">
          <div className="rfgraph rfgraph--explore">
            <ExploreCanvas
              payload={payload}
              mode={viewMode}
              hiddenGroups={hiddenGroups}
              selectedNodeId={selectedNode?.id ?? null}
              selectedEdgeId={selectedEdge?.id ?? null}
              onSelectNode={n => { setSelectedNode(n); if (n) setSelectedEdge(null); }}
              onSelectEdge={e => { setSelectedEdge(e); if (e) setSelectedNode(null); }}
              search={searchQuery}
            />
          </div>
        </main>

        {showDetail && (
          <aside className="explorer__detail">
            <GraphInspector
              node={selectedNode}
              edge={selectedEdge}
              fallbackDocId={docId}
              onViewEvidence={onViewEvidence}
              onClose={() => { setSelectedNode(null); setSelectedEdge(null); }}
            />
          </aside>
        )}
      </div>
    </div>
  );
}
