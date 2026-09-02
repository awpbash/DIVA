import { ReactNode, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { AgentTrace, AssistantTurn, Citation } from "../types";
import { CitationBadge } from "./CitationBadge";
import { InlineSubgraph } from "./InlineSubgraph";
import { IconChevron, IconGraph } from "./Icon";

interface Props {
  turn: AssistantTurn;
  focusedEvidenceId: string | null;
  onCitationClick: (citation: Citation, cluster: Citation[]) => void;
  /** Global "Restricted view" — blur sensitive evidence chips/sources. */
  restricted: boolean;
  /** Open a document in the PDF viewer (for [title](/pdf/<doc_id>) links). */
  onOpenDoc: (docId: string) => void;
}

const INTENT_LABEL: Record<string, string> = {
  factual:     "Looking up the fact",
  comparison:  "Comparing items",
  formula:     "Resolving the formula",
  clause:      "Finding the clause",
  cross_ref:   "Tracing cross-references",
  aggregation: "Gathering results from every document",
  definition:  "Finding the definition",
  other:       "Thinking",
};

/** Split a text string at `[ev:id]` markers and inject CitationBadge components. */
function tokenizeText(
  text: string,
  byId: Map<string, Citation>,
  citations: Citation[],
  focusedEvidenceId: string | null,
  onCitationClick: (c: Citation, cluster: Citation[]) => void,
  restricted: boolean,
): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /\[ev:([A-Za-z0-9_\-:\.]+)\]/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const id = m[1];
    const cite = byId.get(id) ?? null;
    // Skip malformed / unknown citations entirely — the synth model
    // occasionally emits a doc_id-only [ev:xxx] reference that doesn't
    // resolve. Showing it as a `p?` chip is just noise; the citation
    // guard reports the unknown count separately for trust signalling.
    if (cite) {
      out.push(
        <CitationBadge
          key={`${id}-${i++}`}
          citation={cite}
          evidenceId={id}
          active={focusedEvidenceId === id}
          onClick={() => onCitationClick(cite, citations)}
          blurred={restricted && !!cite.sensitivity}
        />,
      );
    }
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** Extract a doc_id from an internal /pdf/<doc_id> link href, else null. */
function pdfDocId(href: string | undefined): string | null {
  if (!href) return null;
  const m = href.match(/\/pdf\/([A-Za-z0-9]+)/);
  return m ? m[1] : null;
}

function AgentTraceStrip({ trace, streaming }: { trace: AgentTrace | undefined; streaming: boolean }) {
  // Default to OPEN while streaming so the user sees tools fire in real
  // time. Collapses on its own once we render the next turn.
  const [open, setOpen] = useState(streaming);
  // Older persisted turns predate the trace field — treat them as empty.
  const steps = trace?.steps ?? [];
  const stepCount = steps.length;
  if (stepCount === 0) return null;
  const totalCalls = steps.reduce((n, s) => n + s.calls.length, 0);
  const inFlight = steps.flatMap(s => s.calls).some(c => c.result === undefined || c.result === null);
  const label = streaming && inFlight
    ? `Agent · step ${stepCount} (${totalCalls} tool call${totalCalls === 1 ? "" : "s"})…`
    : `Agent · ${stepCount} step${stepCount === 1 ? "" : "s"} · ${totalCalls} tool call${totalCalls === 1 ? "" : "s"}` +
      (trace?.finishReason ? ` · ${trace.finishReason.replace("_", " ")}` : "");

  return (
    <div className="agent-trace">
      <button className="agent-trace__head" onClick={() => setOpen(o => !o)} type="button">
        <span className={`agent-trace__dot${inFlight ? " agent-trace__dot--live" : ""}`} />
        <span>{label}</span>
        <div className="agent-trace__spacer" />
        <IconChevron size={12} />
      </button>
      {open && (
        <ol className="agent-trace__body">
          {steps.map(s => (
            <li key={s.step} className="agent-trace__step">
              <div className="agent-trace__step-head">Step {s.step}</div>
              {s.thought && <div className="agent-trace__thought">{s.thought}</div>}
              <ul className="agent-trace__calls">
                {s.calls.map(c => {
                  const pending = c.result === undefined || c.result === null;
                  const err = c.result?.error;
                  return (
                    <li key={c.toolCallId} className={`agent-trace__call${err ? " agent-trace__call--err" : ""}`}>
                      <code className="agent-trace__tool">{c.tool}</code>
                      <span className="agent-trace__args">{formatArgs(c.args)}</span>
                      <span className="agent-trace__result">
                        {pending && "running…"}
                        {!pending && err && `error: ${err}`}
                        {!pending && !err && (
                          c.result?.count !== null && c.result?.count !== undefined
                            ? `count=${c.result.count} · ${c.result.nNew} new`
                            : `${c.result?.nResults ?? 0} hit${c.result?.nResults === 1 ? "" : "s"} · ${c.result?.nNew ?? 0} new`
                        )}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function formatArgs(args: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(args)) {
    if (v === null || v === undefined || v === "") continue;
    if (k === "k") continue;
    let s: string;
    if (typeof v === "string") s = v.length > 30 ? v.slice(0, 30) + "…" : v;
    else if (Array.isArray(v)) s = v.length <= 2 ? v.join(",") : `[${v.length}]`;
    else if (typeof v === "object") s = JSON.stringify(v);
    else s = String(v);
    parts.push(`${k}=${s}`);
  }
  return parts.join(" · ");
}

export function AssistantMessage({
  turn, focusedEvidenceId, onCitationClick, restricted, onOpenDoc,
}: Props) {
  const [showGraph, setShowGraph] = useState(false);
  const byId = useMemo(
    () => new Map(turn.citations.map(c => [c.evidence_id, c])),
    [turn.citations],
  );

  const renderText = (children: ReactNode): ReactNode[] => {
    if (Array.isArray(children)) {
      return children.flatMap((c, i) => {
        if (typeof c === "string") {
          return tokenizeText(c, byId, turn.citations, focusedEvidenceId, onCitationClick, restricted)
            .map((node, j) => <span key={`t-${i}-${j}`}>{node}</span>);
        }
        return [<span key={`x-${i}`}>{c}</span>];
      });
    }
    if (typeof children === "string") {
      return tokenizeText(children, byId, turn.citations, focusedEvidenceId, onCitationClick, restricted);
    }
    return [children];
  };

  // Restricted (sensitive) evidence the answer withheld — surfaced so the user
  // sees the gate AND can showcase the blur (click to open the blurred region).
  const restrictedCites = useMemo(
    () => turn.citations.filter(c => c.sensitivity),
    [turn.citations],
  );

  const isStreaming = turn.status === "streaming" || turn.status === "pending";
  const showThinking = isStreaming && !turn.answer;

  return (
    <div className="msg msg--assistant">
      {turn.plan && (
        <div className="plan-strip">
          <span className="plan-pill plan-pill--intent">{turn.plan.intent.replace("_", " ")}</span>
          {turn.plan.key_terms.slice(0, 5).map(k => (
            <span className="plan-pill" key={k}>{k}</span>
          ))}
          {turn.plan.categories.slice(0, 3).map(c => (
            <span className="plan-pill plan-pill--cat" key={`c-${c}`}>{c}</span>
          ))}
        </div>
      )}

      <AgentTraceStrip
        trace={turn.trace}
        streaming={turn.status === "streaming" || turn.status === "pending"}
      />

      {turn.error && <div className="errbar">⚠ {turn.error}</div>}

      {showThinking ? (
        <div className="thinking">
          <span className="thinking__dots"><span /><span /><span /></span>
          <span>{turn.plan ? INTENT_LABEL[turn.plan.intent] || "Thinking" : "Reading the question"}…</span>
        </div>
      ) : (
        <div className="markdown">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              p:      ({ children }) => <p>{renderText(children)}</p>,
              li:     ({ children }) => <li>{renderText(children)}</li>,
              td:     ({ children }) => <td>{renderText(children)}</td>,
              th:     ({ children }) => <th>{renderText(children)}</th>,
              h1:     ({ children }) => <h1>{renderText(children)}</h1>,
              h2:     ({ children }) => <h2>{renderText(children)}</h2>,
              h3:     ({ children }) => <h3>{renderText(children)}</h3>,
              strong: ({ children }) => <strong>{renderText(children)}</strong>,
              em:     ({ children }) => <em>{renderText(children)}</em>,
              a:      ({ href, children }) => {
                // Internal document links ([title](/pdf/<doc_id>)) open the
                // PDF viewer instead of navigating away.
                const docId = pdfDocId(href);
                if (docId) {
                  return (
                    <button
                      type="button"
                      className="doc-link"
                      onClick={() => onOpenDoc(docId)}
                      title="Open document"
                    >
                      <span className="doc-link__icon">📄</span>
                      {children}
                    </button>
                  );
                }
                return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
              },
            }}
          >
            {turn.answer || "​"}
          </ReactMarkdown>
          {isStreaming && <span className="caret" />}
        </div>
      )}

      {turn.status === "done" && restricted && restrictedCites.length > 0 && (
        <div className="redaction-strip" title="Hidden by your access level">
          <span className="redaction-strip__lock">🔒</span>
          <span className="redaction-strip__label">
            {restrictedCites.length} financial item(s) hidden for your access level
          </span>
          <span className="redaction-strip__chips">
            {restrictedCites.slice(0, 8).map(c => (
              <CitationBadge
                key={c.evidence_id}
                citation={c}
                evidenceId={c.evidence_id}
                active={focusedEvidenceId === c.evidence_id}
                onClick={() => onCitationClick(c, restrictedCites)}
                blurred
              />
            ))}
            {restrictedCites.length > 8 && (
              <span className="redaction-strip__more">
                +{restrictedCites.length - 8} more
              </span>
            )}
          </span>
        </div>
      )}

      {turn.unknownCitations && turn.unknownCitations.length > 0 && (
        <div className="errbar">
          {turn.unknownCitations.length} reference(s) in this answer could not
          be matched to a source document. Treat those parts with caution.
        </div>
      )}

      {turn.status === "done" && !turn.error && (
        <div className="msg__actions">
          <button
            className="msg-report"
            type="button"
            title="Report this answer to the developer"
            onClick={() => window.dispatchEvent(new CustomEvent("feedback:open", {
              detail: {
                category: "answer",
                context: { reported_answer: (turn.answer || "").slice(0, 1500) },
              },
            }))}
          >
            ⚑ Report this answer
          </button>
        </div>
      )}

      {turn.status === "done" && (() => {
        // Show only the citations the answer actually cited — the wider
        // retrieved bundle is debug noise that confuses end users. If
        // nothing was cited, hide the whole subgraph panel.
        const usedSet = new Set(turn.usedCitations || []);
        const usedCitations = turn.citations.filter(c => usedSet.has(c.evidence_id));
        if (usedCitations.length === 0) return null;
        return (
          <div className="subgraph">
            <button className="subgraph__head" onClick={() => setShowGraph(s => !s)}>
              <IconGraph />
              <span>Sources · {usedCitations.length}</span>
              <div className="subgraph__head-spacer" />
              <IconChevron size={14} />
            </button>
            {showGraph && (
              <div className="subgraph__body">
                <InlineSubgraph
                  citations={usedCitations}
                  focusedEvidenceId={focusedEvidenceId}
                  onCitationClick={onCitationClick}
                />
              </div>
            )}
          </div>
        );
      })()}
    </div>
  );
}
