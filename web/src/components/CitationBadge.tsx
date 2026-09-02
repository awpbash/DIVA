import { Citation } from "../types";

interface Props {
  citation: Citation | null;
  evidenceId: string;
  active: boolean;
  onClick: () => void;
  /** When true, this evidence is restricted for the current viewer role —
   * render it blurred + locked and suppress the snippet tooltip. */
  blurred?: boolean;
}

export function CitationBadge({ citation, evidenceId, active, onClick, blurred }: Props) {
  // Show "1" / "2" / ... ? The user sees plenty of badges, a compact label is
  // the section number when known, falling back to a short evidence suffix.
  const label = blurred
    ? "🔒"
    : citation?.section_num
      ? `§${citation.section_num}`
      : `p${citation?.page_no ?? "?"}`;
  // Document review status stays tooltip-only. Putting a ✓ on the chip for it
  // read as "this value is verified" and clashed with the field-consensus
  // green — one glyph, two meanings. On-chip marks are field-level only.
  const trust = !blurred && citation?.doc_trust && citation.doc_trust !== "unreviewed"
    ? citation.doc_trust : null;
  let trustNote = trust
    ? ` · document ${trust === "reviewed" ? "fully" : "partly"} human-verified` +
      ` (${citation?.doc_verified}/${citation?.doc_populated} fields)`
    : "";
  // Field-level consensus (knowledge-base citations): the strongest signal,
  // so it leads — exactly who stands behind THIS value and the vote share.
  if (!blurred && citation?.field_trust === "human_validated" && citation.verified_by?.length) {
    const pct = citation.verify_confidence != null
      ? `, ${Math.round(citation.verify_confidence * 100)}%` : "";
    const votes = citation.verify_votes
      ? ` (${citation.verified_by.length}/${citation.verify_votes} votes${pct})` : "";
    trustNote = ` · verified by ${citation.verified_by.join(", ")}${votes}${trustNote}`;
  } else if (!blurred && citation?.field_trust === "disputed") {
    trustNote = ` · disputed: verifiers disagree on this value${trustNote}`;
  }
  // Field consensus is shown ON the chip (green tint + vote share), not
  // tooltip-only — hover-only trust was too easy to miss.
  const fieldVerified = !blurred && citation?.field_trust === "human_validated";
  const fieldDisputed = !blurred && citation?.field_trust === "disputed";
  const votePct = fieldVerified && citation?.verify_confidence != null
    ? `${Math.round(citation.verify_confidence * 100)}%` : "";
  // Suppress the snippet in the tooltip when restricted — the tooltip is a
  // leak path. (Real enforcement redacts server-side; this is the demo layer.)
  const title = blurred
    ? "Restricted. Hidden for your access level."
    : citation
      ? `${citation.section_num ? "Section " + citation.section_num + " · " : ""}page ${citation.page_no}${trustNote}: ${citation.snippet.slice(0, 200)}`
      : `Unknown evidence: ${evidenceId}`;
  return (
    <span
      className={`cite ${active ? "cite--active" : ""} ${blurred ? "cite--blur" : ""}` +
        `${fieldVerified ? " cite--verified" : ""}${fieldDisputed ? " cite--disputed" : ""}`}
      onClick={onClick}
      onKeyDown={e => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick();
        }
      }}
      role="button"
      tabIndex={0}
      aria-label={blurred ? "Restricted citation. Hidden for your access level." : title}
      title={title}
    >
      {label}
      {fieldVerified && <span className="cite__vote">✓{votePct && ` ${votePct}`}</span>}
      {fieldDisputed && <span className="cite__vote">⚠</span>}
    </span>
  );
}
