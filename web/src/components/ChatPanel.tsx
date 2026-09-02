import { useCallback, useEffect, useRef, useState } from "react";
import { streamChat } from "../api";
import { AssistantTurn, ChatMessage, Citation } from "../types";
import { Thread } from "../hooks/useThreads";
import { AssistantMessage } from "./AssistantMessage";
import { IconSend, IconPanelRight } from "./Icon";
import { loadUi, saveUi } from "../uiState";

interface Props {
  /** Contracts in the search scope. Empty = all (cross-doc). */
  docIds: string[];
  thread: Thread | null;
  onUpdateThread: (id: string, patch: Partial<Thread> | ((t: Thread) => Thread)) => void;
  onFocusEvidence: (citation: Citation, cluster: Citation[]) => void;
  /** Whether the source PDF panel is currently visible alongside this. */
  pdfVisible: boolean;
  /** Bring the PDF panel back. Shown as a floating pill when hidden. */
  onShowPdf: () => void;
  /** Global "Restricted view" — blur sensitive evidence in answers. The role
   * itself is server-derived from the login session; nothing is sent here. */
  restricted: boolean;
  /** Open a document in the PDF viewer (doc-link clicks). */
  onOpenDoc: (docId: string) => void;
}

interface Suggestion {
  intent: string;
  text: string;
}

// Plain-language starter questions: labels a non-technical user would say,
// not retrieval jargon (was: Factual / Cross-ref / Aggregation).
//
// Deliberately domain-neutral. These used to name one deployment's own fields
// ("consumption charge rate", "default-interest formula"), which read as
// nonsense in any other instance and advertised concepts the loaded schema did
// not have. Each one now demonstrates a KIND of question the two retrieval
// modes handle, which is the useful thing to show and is true everywhere.
const SUGGESTIONS: Suggestion[] = [
  { intent: "Look up a value",    text: "What is the current fee, and which document set it?" },
  { intent: "Compare documents",  text: "Compare the termination terms across the loaded documents." },
  { intent: "Add things up",      text: "What do the amounts payable total?" },
  { intent: "Trace a clause",     text: "What does the governing law clause say, word for word?" },
  { intent: "List across docs",   text: "List every obligation across the documents." },
  { intent: "Check a definition", text: "What is the defined meaning of a term used here?" },
];

export function ChatPanel({
  docIds, thread, onUpdateThread, onFocusEvidence, pdfVisible, onShowPdf,
  restricted, onOpenDoc,
}: Props) {
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  // An unsent question survives tab switches (this panel unmounts) and thread
  // switches, per thread. Cleared once the question is actually sent.
  const draftKey = `chat.draft.${thread?.id ?? "none"}`;
  useEffect(() => { setInput(loadUi(draftKey, "")); }, [draftKey]);
  const [focusedEvidenceId, setFocusedEvidenceId] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const abortRef = useRef<{ abort: () => void } | null>(null);

  // Auto-scroll when content grows
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [thread?.turns]);

  // Auto-grow the composer up to the CSS max-height, then scroll internally —
  // a single-row textarea makes multi-line questions unreadable while typing.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [input]);

  const handleCitationClick = useCallback(
    (c: Citation, cluster: Citation[]) => {
      setFocusedEvidenceId(c.evidence_id);
      onFocusEvidence(c, cluster);
    },
    [onFocusEvidence],
  );

  const ask = useCallback(
    (question: string) => {
      if (!question.trim() || streaming || !thread) return;
      const trimmed = question.trim();
      const turnId = `t-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      const newUserMsg: ChatMessage = { role: "user", content: trimmed };

      // Build the messages array NOW so we don't race with thread state.
      const allMessages: ChatMessage[] = [];
      for (let i = 0; i < thread.userHistory.length; i++) {
        allMessages.push(thread.userHistory[i]);
        const matchedTurn = thread.turns[i];
        if (matchedTurn?.status === "done") {
          allMessages.push({ role: "assistant", content: matchedTurn.answer });
        }
      }
      allMessages.push(newUserMsg);

      const newTurn: AssistantTurn = {
        id: turnId,
        question: trimmed,
        answer: "",
        plan: null,
        citations: [],
        status: "pending",
        trace: { steps: [], stepsUsed: 0, finishReason: null, nCitations: 0 },
      };

      onUpdateThread(thread.id, t => ({
        ...t,
        userHistory: [...t.userHistory, newUserMsg],
        turns: [...t.turns, newTurn],
      }));
      setInput("");
      saveUi(`chat.draft.${thread.id}`, "");
      setStreaming(true);

      const patchTurn = (patch: Partial<AssistantTurn>) =>
        onUpdateThread(thread.id, t => ({
          ...t,
          turns: t.turns.map(x => (x.id === turnId ? { ...x, ...patch } : x)),
        }));

      let answerSoFar = "";

      const patchTrace = (mutate: (t: AssistantTurn) => AssistantTurn) =>
        onUpdateThread(thread.id, t => ({
          ...t,
          turns: t.turns.map(x => (x.id === turnId ? mutate(x) : x)),
        }));

      const handle = streamChat(
        allMessages,
        {
          onPlan:      plan => patchTurn({ plan, status: "streaming" }),
          onAgentStep: s => patchTrace(turn => ({
            ...turn,
            status: "streaming",
            trace: {
              ...turn.trace,
              steps: [
                ...turn.trace.steps,
                {
                  step: s.step,
                  thought: s.thought,
                  calls: s.tool_calls.map((tc, i) => ({
                    // tool_call event will replace this id once it arrives,
                    // but emit a stable temp id so React keys are happy.
                    toolCallId: `step-${s.step}-${i}-${tc.name}`,
                    tool: tc.name,
                    args: tc.args,
                  })),
                },
              ],
            },
          })),
          onAgentToolCall: tc => patchTrace(turn => ({
            ...turn,
            trace: {
              ...turn.trace,
              steps: turn.trace.steps.map(step => {
                if (step.step !== tc.step) return step;
                // Match by tool name within this step — index unstable if
                // duplicate tool names, but rare enough to ignore.
                let matched = false;
                const calls = step.calls.map(c => {
                  if (matched) return c;
                  if (c.tool === tc.tool && !c.toolCallId.startsWith("real-")) {
                    matched = true;
                    return { ...c, toolCallId: `real-${tc.tool_call_id}`,
                             args: tc.args, result: null };
                  }
                  return c;
                });
                if (!matched) {
                  calls.push({
                    toolCallId: `real-${tc.tool_call_id}`,
                    tool: tc.tool, args: tc.args, result: null,
                  });
                }
                return { ...step, calls };
              }),
            },
          })),
          onAgentToolResult: r => patchTrace(turn => ({
            ...turn,
            trace: {
              ...turn.trace,
              steps: turn.trace.steps.map(step => {
                if (step.step !== r.step) return step;
                return {
                  ...step,
                  calls: step.calls.map(c => {
                    if (c.toolCallId !== `real-${r.tool_call_id}`) return c;
                    return {
                      ...c,
                      result: {
                        nResults: r.n_results,
                        nNew:     r.n_new,
                        nTotal:   r.n_total,
                        count:    r.count,
                        error:    r.error,
                      },
                    };
                  }),
                };
              }),
            },
          })),
          onAgentDone: d => patchTrace(turn => ({
            ...turn,
            trace: { ...turn.trace,
              stepsUsed: d.steps_used,
              finishReason: d.finish_reason,
              nCitations: d.n_citations },
          })),
          onCitations: citations => patchTurn({ citations, status: "streaming" }),
          onToken:     delta => {
            answerSoFar += delta;
            const localCopy = answerSoFar;
            onUpdateThread(thread.id, t => ({
              ...t,
              turns: t.turns.map(x =>
                x.id === turnId ? { ...x, answer: localCopy, status: "streaming" } : x,
              ),
            }));
          },
          onDone: info => {
            patchTurn({
              status: "done",
              usedCitations:    info.used_citations,
              unknownCitations: info.unknown_citations,
            });
            setStreaming(false);
            abortRef.current = null;
          },
          onError: msg => {
            patchTurn({ status: "error", error: msg });
            setStreaming(false);
            abortRef.current = null;
          },
        },
        { docIds: docIds.length ? docIds : undefined },
      );
      abortRef.current = handle;
    },
    [docIds, streaming, thread, onUpdateThread],
  );

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    ask(input);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      ask(input);
    }
  };

  const turns = thread?.turns ?? [];
  const userHistory = thread?.userHistory ?? [];
  const empty = turns.length === 0;

  return (
    <div className="pane">
      {!pdfVisible && (
        <button
          type="button"
          className="pdf-toggle"
          onClick={onShowPdf}
          title="Show source PDF"
          aria-label="Show source PDF"
        >
          <IconPanelRight size={14} />
          <span>Source</span>
        </button>
      )}
      <div className="chat">
        <div className="chat__scroll" ref={scrollRef}>
          <div className="chat__inner">
            {empty ? (
              <div className="empty">
                <div className="empty__hero" />
                <h1>Ask about your documents.</h1>
                <p>
                  Every answer comes straight from the documents and cites its
                  source. Click a citation chip to see the exact clause
                  highlighted on the right. Pick documents in the sidebar to
                  narrow the search.
                </p>
                <div className="suggestions">
                  {SUGGESTIONS.map(s => (
                    <button
                      key={s.text}
                      className="suggestion"
                      onClick={() => ask(s.text)}
                    >
                      <div className="suggestion__intent">{s.intent}</div>
                      <div>{s.text}</div>
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              turns.map((turn, idx) => {
                const userMsg = userHistory[idx];
                return (
                  <div key={turn.id} className="fade-in">
                    {userMsg && (
                      <div className="msg msg--user">
                        <div className="msg__bubble">{userMsg.content}</div>
                      </div>
                    )}
                    <AssistantMessage
                      turn={turn}
                      focusedEvidenceId={focusedEvidenceId}
                      onCitationClick={handleCitationClick}
                      restricted={restricted}
                      onOpenDoc={onOpenDoc}
                    />
                  </div>
                );
              })
            )}
          </div>
        </div>

        <form className="composer" onSubmit={onSubmit}>
          <div className="composer__inner">
            <textarea
              ref={textareaRef}
              className="composer__textarea"
              placeholder={
                empty
                  ? "Ask about the documents, e.g. “How long does this agreement run for?”"
                  : "Ask a follow-up…"
              }
              value={input}
              onChange={e => { setInput(e.target.value); saveUi(draftKey, e.target.value); }}
              onKeyDown={onKeyDown}
              rows={1}
              disabled={streaming}
            />
            <div className="composer__row">
              <div className="composer__row-spacer" />
              <button
                type="submit"
                className="composer__send"
                disabled={streaming || !input.trim()}
                aria-label="Send"
                title="Send (Enter)"
              >
                <IconSend />
              </button>
            </div>
          </div>
          <div className="composer__hint">
            Enter to send · Shift+Enter for a new line · Every answer cites its source.
          </div>
        </form>
      </div>
    </div>
  );
}
