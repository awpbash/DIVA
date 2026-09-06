/**
 * Explainability panel for the Explore graph.
 *
 * Shows the selected NODE's properties + provenance (source document, page,
 * evidence snippet) with a "View in PDF" jump that reuses the citation/bbox
 * machinery — or the selected EDGE's justification (method / confidence /
 * signals) for asserted cross-document links (RESOLVES_TO).
 *
 * Nothing here is invented: every value is read off the node/edge the graph
 * returned. When provenance is absent we say so plainly rather than guess —
 * "not in the documents" is a correct answer.
 */
import { useEffect, useState } from "react";
import { getEvidence, kmPageUrl } from "../api";
import { EvidenceDetail, GraphEdge, GraphNode } from "../types";
import { AuthedImage } from "./AuthedImage";
import { groupColorVar, groupForLabel, isProposalNode } from "./graphTheme";
import { IconClose, IconDocument } from "./Icon";


// Props folded onto nodes by the API for provenance / trust; surfaced
// specially rather than dumped in the generic property list.
const PROVENANCE_KEYS = new Set([
  "evidence_id", "page_no", "snippet", "n_evidence",
]);
const HIDE_KEYS = new Set([
  "embedding", "embed_text", "embed_text_hash",
]);


interface Props {
  node: GraphNode | null;
  edge: GraphEdge | null;
  /** doc scope, used when a node doesn't carry its own doc_id. */
  fallbackDocId: string | null;
  onViewEvidence?: (evidenceId: string, docId: string) => void;
  onClose: () => void;
}


function fmt(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) return v.map(fmt).join(", ");
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function readableLabel(label: string): string {
  if (label === "CanonicalParty") return "Party";
  return label.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[_-]+/g, " ");
}

function readableRole(role: unknown): string {
  return typeof role === "string"
    ? role.replace(/([a-z])([A-Z])/g, "$1 $2").replace(/[_-]+/g, " ")
    : "";
}


export function GraphInspector({ node, edge, fallbackDocId, onViewEvidence, onClose }: Props) {
  if (edge) return <EdgeInspector edge={edge} onClose={onClose} />;
  if (node) {
    return (
      <NodeInspector
        node={node}
        fallbackDocId={fallbackDocId}
        onViewEvidence={onViewEvidence}
        onClose={onClose}
      />
    );
  }
  return null;
}


function PanelHead({ onClose, children }: { onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="inspector__head">
      <div className="inspector__head-main">{children}</div>
      <button
        type="button"
        className="pane__icon-btn"
        onClick={onClose}
        aria-label="Close inspector"
        title="Close"
      >
        <IconClose size={13} />
      </button>
    </div>
  );
}


function NodeInspector({
  node, fallbackDocId, onViewEvidence, onClose,
}: {
  node: GraphNode;
  fallbackDocId: string | null;
  onViewEvidence?: (evidenceId: string, docId: string) => void;
  onClose: () => void;
}) {
  const group = groupForLabel(node.label);
  const proposal = isProposalNode(node);
  const props = node.props || {};

  const evidenceId = props["evidence_id"] as string | undefined;
  const pageNo = props["page_no"];
  const snippet = props["snippet"] as string | undefined;
  const nEvidence = props["n_evidence"] as number | undefined;
  const docId = (props["doc_id"] as string | undefined) || fallbackDocId || "";
  const confidence = props["confidence"];

  const generalRows = Object.entries(props).filter(
    ([k, v]) =>
      !PROVENANCE_KEYS.has(k) && !HIDE_KEYS.has(k) &&
      k !== "doc_id" && k !== "confidence" && k !== "status" &&
      v !== null && v !== undefined && v !== "",
  );

  return (
    <>
      <PanelHead onClose={onClose}>
        <span
          className="chip chip--label"
          style={{ color: groupColorVar(group), borderColor: "transparent" }}
        >
          {readableLabel(node.label)}
        </span>
        {proposal && <span className="inspector__flag inspector__flag--warn">unverified</span>}
        {typeof confidence === "number" && (
          <span className="inspector__flag">conf {confidence}</span>
        )}
      </PanelHead>

      <h3 className="inspector__title">{node.title}</h3>

      {/* Provenance — the trust spine */}
      {(evidenceId || pageNo !== undefined || snippet) && (
        <div className="inspector__section">
          <div className="inspector__section-h">Provenance</div>
          {pageNo !== undefined && pageNo !== null && (
            <div className="detail-row">
              <div className="detail-row__key">page</div>
              <div className="detail-row__val">p{fmt(pageNo)}</div>
            </div>
          )}
          {typeof nEvidence === "number" && nEvidence > 1 && (
            <div className="detail-row">
              <div className="detail-row__key">evidence</div>
              <div className="detail-row__val">{nEvidence} spans</div>
            </div>
          )}
          {snippet && (
            <blockquote className="inspector__snippet">“{snippet}”</blockquote>
          )}
          {evidenceId && docId && <EvidencePreview evidenceId={evidenceId} docId={docId} />}
          {evidenceId && docId && onViewEvidence && (
            <button
              type="button"
              className="inspector__pdf-btn"
              onClick={() => onViewEvidence(evidenceId, docId)}
            >
              <IconDocument size={14} />
              <span>View highlight in PDF</span>
            </button>
          )}
          {!evidenceId && (
            <div className="inspector__muted">
              No source clause for this node. It's structural, not extracted text.
            </div>
          )}
        </div>
      )}

      {/* Generic properties */}
      {generalRows.length > 0 && (
        <div className="inspector__section">
          <div className="inspector__section-h">Properties</div>
          {generalRows.map(([k, v]) => (
            <div className="detail-row" key={k}>
              <div className="detail-row__key">{k}</div>
              <div className="detail-row__val">{fmt(v)}</div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}


// A cropped page image with the clause highlighted, right in the panel — the
// same crop the "View highlight in PDF" jump opens full-size, so checking a
// fact's source doesn't cost leaving the tab. Same machinery as the
// Knowledge tab's evidence pane (kmPageUrl + AuthedImage + percentage rects).
function EvidencePreview({ evidenceId, docId }: { evidenceId: string; docId: string }) {
  const [ev, setEv] = useState<EvidenceDetail | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    setEv(null);
    setFailed(false);
    getEvidence(evidenceId)
      .then(e => { if (alive) setEv(e); })
      .catch(() => { if (alive) setFailed(true); });
    return () => { alive = false; };
  }, [evidenceId]);

  if (failed) return null; // the PDF jump below still works
  if (!ev) return <div className="inspector__ev-loading" />;

  const rects = (ev.rects ?? []).filter(r => r.page_no === ev.page_no);
  if (!rects.length) return null;

  return (
    <div className="inspector__ev-page">
      <AuthedImage src={kmPageUrl(docId, ev.page_no)} alt={`page ${ev.page_no}`} />
      {rects.map((r, i) => {
        const [x0, y0, x1, y1] = r.bbox;
        return (
          <div
            key={i}
            className="inspector__ev-hl"
            style={{
              left: `${x0 * 100}%`, top: `${y0 * 100}%`,
              width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`,
            }}
          />
        );
      })}
    </div>
  );
}


function EdgeInspector({ edge, onClose }: { edge: GraphEdge; onClose: () => void }) {
  const props = edge.props || {};
  const method = props["method"];
  const confidence = props["confidence"];
  const signals = props["signals"];
  const sharedEntity = edge.type === "HAS_ENTITY";
  const partyRelation = edge.type === "HAS_PARTY";
  const asserted = edge.type === "RESOLVES_TO" || sharedEntity || partyRelation;
  const mentionCount = props["mentions"] ?? props["facts"];
  const fields = props["fields"] ?? props["names"];
  const role = props["role"];
  const current = props["current"];
  const evidence = props["evidence"];

  const otherRows = Object.entries(props).filter(
    ([k, v]) =>
      k !== "method" && k !== "confidence" && k !== "signals" &&
      k !== "facts" && k !== "names" && k !== "mentions" &&
      k !== "fields" && k !== "role" && k !== "current" && k !== "evidence" &&
      v !== null && v !== undefined && v !== "",
  );

  return (
    <>
      <PanelHead onClose={onClose}>
        <span className="chip chip--label" style={{ borderColor: "transparent" }}>
          {partyRelation ? "party relationship" : sharedEntity ? "derived entity link" : edge.type.replace(/_/g, " ")}
        </span>
      </PanelHead>

      <h3 className="inspector__title">
        {partyRelation ? "Named party" : sharedEntity ? "Derived entity link" : asserted ? "Matched relationship" : "Structural relationship"}
      </h3>

      {asserted ? (
        <div className="inspector__section">
          <div className="inspector__section-h">{partyRelation || sharedEntity ? "How this relationship is supported" : "Why these were linked"}</div>
          {partyRelation && (
            <div className="inspector__muted">
              This document names the connected party{readableRole(role) ? ` as ${readableRole(role)}` : ""}. The party node collects every document that names the same entity.
            </div>
          )}
          {sharedEntity && (
            <div className="inspector__muted">
              This fallback link groups knowledge fields that refer to the same entity.
            </div>
          )}
          {role !== undefined && (
            <div className="detail-row">
              <div className="detail-row__key">role</div>
              <div className="detail-row__val">{readableRole(role) || fmt(role)}</div>
            </div>
          )}
          {current !== undefined && (
            <div className="detail-row">
              <div className="detail-row__key">family status</div>
              <div className="detail-row__val">{current ? "current" : "historic"}</div>
            </div>
          )}
          {mentionCount !== undefined && (
            <div className="detail-row">
              <div className="detail-row__key">knowledge fields</div>
              <div className="detail-row__val">{fmt(mentionCount)}</div>
            </div>
          )}
          {fields !== undefined && (
            <div className="detail-row">
              <div className="detail-row__key">source fields</div>
              <div className="detail-row__val">{fmt(fields)}</div>
            </div>
          )}
          {evidence !== undefined && (
            <div className="detail-row">
              <div className="detail-row__key">evidence-backed</div>
              <div className="detail-row__val">{fmt(evidence)}</div>
            </div>
          )}
          {!sharedEntity && method !== undefined && (
            <div className="detail-row">
              <div className="detail-row__key">method</div>
              <div className="detail-row__val">{fmt(method)}</div>
            </div>
          )}
          {!sharedEntity && confidence !== undefined && (
            <div className="detail-row">
              <div className="detail-row__key">confidence</div>
              <div className="detail-row__val">{fmt(confidence)}</div>
            </div>
          )}
          {!sharedEntity && signals !== undefined && (
            <div className="detail-row">
              <div className="detail-row__key">signals</div>
              <div className="detail-row__val">{fmt(signals)}</div>
            </div>
          )}
          {!sharedEntity && method === undefined && confidence === undefined && signals === undefined && (
            <div className="inspector__muted">
              No explanation recorded for this link.
            </div>
          )}
        </div>
      ) : (
        <div className="inspector__muted">
          Created automatically from the document structure.
        </div>
      )}

      {otherRows.length > 0 && (
        <div className="inspector__section">
          <div className="inspector__section-h">Edge properties</div>
          {otherRows.map(([k, v]) => (
            <div className="detail-row" key={k}>
              <div className="detail-row__key">{k}</div>
              <div className="detail-row__val">{fmt(v)}</div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
