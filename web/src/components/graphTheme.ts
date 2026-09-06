/**
 * How the Explore graph colours and groups nodes.
 *
 * The mapping is SERVED, not written here: `GET /graph/legend` builds it from
 * the active pack, so a deployment with its own fact types and identity hubs
 * gets a correct legend for free.
 *
 * This file used to hold a literal list of one domain's labels. On any other
 * domain the labels it did not know rendered in the unclassified grey while the
 * legend advertised labels that did not exist. Same shape of bug as the
 * hardcoded category order in the knowledge and review views.
 *
 * The fallback below is the part that genuinely cannot vary: the structural
 * spine every pack inherits from `_base.yaml`. It covers the first paint, and
 * the login screen and any offline state, exactly like `branding.ts`.
 */
import { useEffect, useState } from "react";

import { apiFetch, apiUrl, authHeaders } from "../api";
import { GraphNode } from "../types";

/** A group key. Open, because a pack declares its own list. */
export type NodeGroup = string;

export interface LegendEntry {
  group: NodeGroup;
  label: string;
  hint: string;
}

export interface GraphLegend {
  /** Legend rows, in display order. */
  groups: LegendEntry[];
  /** Node label → group key. */
  labels: Record<string, NodeGroup>;
}

/** The structural spine from `_base.yaml`, which no pack changes. */
const FALLBACK: GraphLegend = {
  groups: [
    { group: "document", label: "Document", hint: "the document spine" },
    { group: "section", label: "Section", hint: "clause structure" },
    { group: "fact", label: "Fact", hint: "extracted facts" },
    { group: "identity", label: "Canonical entity", hint: "one entity shared across documents" },
    { group: "evidence", label: "Evidence", hint: "the snippet behind a value" },
    { group: "proposal", label: "Proposal", hint: "unverified, awaiting review" },
  ],
  labels: {
    Document: "document", Agreement: "document", DocAsset: "document",
    Section: "section", Block: "section",
    Fact: "fact",
    EvidenceSpan: "evidence", FactMention: "evidence",
    Proposal: "proposal",
  },
};

// Module-level cache, same pattern as branding: the first component to mount
// pays for the request and every later mount renders the real legend at once.
let cached: GraphLegend = FALLBACK;
let inflight: Promise<GraphLegend> | null = null;

export function loadGraphLegend(): Promise<GraphLegend> {
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const r = await apiFetch(apiUrl("/graph/legend"), { headers: authHeaders() });
      if (r.ok) {
        const j = await r.json();
        const groups: LegendEntry[] = (j.groups || []).map(
          (g: { key: string; label: string; hint: string }) => ({
            group: g.key, label: g.label, hint: g.hint,
          }));
        if (groups.length) {
          cached = { groups, labels: { ...FALLBACK.labels, ...(j.labels || {}) } };
        }
      }
    } catch { /* keep the fallback — the graph still renders, just plainer */ }
    return cached;
  })();
  return inflight;
}

/** The legend as currently known. Synchronous, for render paths. */
export const graphLegend = (): GraphLegend => cached;

/** React hook: the legend, refreshing once the fetch lands. */
export function useGraphLegend(): GraphLegend {
  const [legend, setLegend] = useState<GraphLegend>(cached);
  useEffect(() => { loadGraphLegend().then(setLegend); }, []);
  return legend;
}

/** Which group a node label belongs to. Unknown labels are "other". */
export function groupForLabel(label: string): NodeGroup {
  return cached.labels[label] || "other";
}

/** Every label in a group, e.g. all fact labels for the layout's fact column. */
export function labelsInGroup(group: NodeGroup): Set<string> {
  return new Set(Object.entries(cached.labels)
    .filter(([, g]) => g === group)
    .map(([l]) => l));
}

/** CSS custom-property reference for a group's colour. */
export function groupColorVar(group: string): string {
  const map: Record<string, string> = {
    document: "var(--accent)",
    section: "var(--primary)",
    fact: "var(--teal)",
    identity: "var(--pink)",
    evidence: "var(--warn)",
    proposal: "var(--error)",
    other: "var(--text-subtle)",
  };
  return map[group] || map.other;
}

/** A node is "proposal" / unverified either by label or by a status prop. */
export function isProposalNode(n: GraphNode): boolean {
  if (n.label === "Proposal") return true;
  const status = (n.props?.["status"] as string) || "";
  return status === "pending" || status === "proposed";
}

/** Plain-text haystack for the Explore search box: title + type + every prop
 * value, lower-cased once so a query is a single substring check. Nothing
 * fancy — the payload's already in memory, so this is what "queryable" means
 * for a graph this size. */
export function nodeSearchText(n: GraphNode): string {
  const propsText = Object.values(n.props || {})
    .map(v => (v === null || v === undefined) ? ""
      : typeof v === "object" ? JSON.stringify(v) : String(v))
    .join(" ");
  return `${n.title} ${n.label} ${propsText}`.toLowerCase();
}
