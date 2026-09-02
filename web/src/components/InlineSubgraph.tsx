/**
 * Per-answer subgraph — the small panel under each assistant turn.
 *
 * Renders the cited-evidence slice through `GraphCanvas` in "inline"
 * mode: everything pre-expanded since the slice is already narrow.
 * Clicking an evidence leaf routes the click back to the chat so the
 * PDF highlight follows.
 */
import { useEffect, useState } from "react";
import { getSubgraph } from "../api";
import { Citation, GraphNode, GraphPayload } from "../types";
import { GraphCanvas } from "./GraphCanvas";


interface Props {
  citations: Citation[];
  focusedEvidenceId: string | null;
  onCitationClick: (c: Citation, cluster: Citation[]) => void;
}


export function InlineSubgraph({ citations, focusedEvidenceId, onCitationClick }: Props) {
  const [payload, setPayload] = useState<GraphPayload>({ nodes: [], edges: [] });

  // Key on the joined id string, not the array reference — `citations`
  // is recomputed via filter() on every parent render, so depending on
  // the array directly re-fetches on every keystroke / streamed turn.
  const idsKey = citations.map(c => c.evidence_id).sort().join(",");

  useEffect(() => {
    if (!idsKey) {
      setPayload({ nodes: [], edges: [] });
      return;
    }
    let cancelled = false;
    getSubgraph(idsKey.split(","))
      .then(p => { if (!cancelled) setPayload(p); })
      .catch(err => console.error("getSubgraph:", err));
    return () => { cancelled = true; };
  }, [idsKey]);

  const byEvidenceId = new Map(citations.map(c => [c.evidence_id, c]));

  const handleLeaf = (raw: GraphNode) => {
    const cite = byEvidenceId.get(raw.id);
    if (cite) onCitationClick(cite, citations);
  };

  return (
    <div className="rfgraph rfgraph--inline">
      <GraphCanvas
        payload={payload}
        mode="inline"
        focusedEvidenceId={focusedEvidenceId}
        onLeafClick={handleLeaf}
      />
    </div>
  );
}
