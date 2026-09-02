import { useCallback, useEffect, useRef, useState } from "react";
import {
  FeedbackCategory, MyFeedbackItem, getMyFeedback, postFeedback,
  uploadFeedbackImages,
} from "../api";
import { Thread } from "../hooks/useThreads";
import "./FeedbackButton.css";

interface Props {
  tab: string;
  thread: Thread | null;
  viewerDocId: string | null;
  selectedDocIds: string[];
}

const CATEGORIES: { key: FeedbackCategory; label: string }[] = [
  { key: "ui_ux", label: "UI / UX" },
  { key: "extraction", label: "Extraction" },
  { key: "answer", label: "Wrong answer" },
  { key: "other", label: "Other" },
];

const MAX_SHOTS = 3;
const MAX_SHOT_BYTES = 8 * 1024 * 1024;

/** Floating feedback capture. Also listens for `feedback:open` events
 * (dispatched by the per-answer Report flag) to open pre-filled — the same
 * window-event pattern `auth:expired` uses, so no prop drilling. Screenshots
 * can be picked with the 📎 button or pasted straight into the text box. */
export function FeedbackButton({ tab, thread, viewerDocId, selectedDocIds }: Props) {
  const [open, setOpen] = useState(false);
  const [category, setCategory] = useState<FeedbackCategory>("ui_ux");
  const [message, setMessage] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "sent" | "error">("idle");
  const [shotErr, setShotErr] = useState<string | null>(null);
  // Screenshots staged locally (uploaded only on Send, so an abandoned panel
  // leaves nothing on the server). preview = object URL for the thumbnail.
  const [shots, setShots] = useState<{ file: File; preview: string }[]>([]);
  const shotRef = useRef<HTMLInputElement>(null);
  // Extra context handed over by a "Report" flag (answer text etc.).
  const extraCtx = useRef<Record<string, unknown> | null>(null);
  // The reporter's own past reports with triage status — people see whether
  // something they filed was acknowledged or fixed (loop closure).
  const [mine, setMine] = useState<MyFeedbackItem[]>([]);

  useEffect(() => {
    if (!open) return;
    getMyFeedback().then(setMine).catch(() => {});
  }, [open]);

  useEffect(() => {
    const onOpen = (e: Event) => {
      const detail = (e as CustomEvent).detail || {};
      if (detail.category) setCategory(detail.category as FeedbackCategory);
      extraCtx.current = detail.context ?? null;
      setState("idle");
      setOpen(true);
    };
    window.addEventListener("feedback:open", onOpen);
    return () => window.removeEventListener("feedback:open", onOpen);
  }, []);

  // Object URLs leak unless revoked — clean up whichever are left on unmount.
  useEffect(() => () => { shots.forEach(s => URL.revokeObjectURL(s.preview)); },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    []);

  const addShots = useCallback((files: File[]) => {
    setShotErr(null);
    setShots(prev => {
      const next = [...prev];
      for (const f of files) {
        if (!f.type.startsWith("image/")) { setShotErr("Only images can be attached."); continue; }
        if (f.size > MAX_SHOT_BYTES) { setShotErr("That image is over 8 MB."); continue; }
        if (next.length >= MAX_SHOTS) { setShotErr(`At most ${MAX_SHOTS} screenshots.`); break; }
        next.push({ file: f, preview: URL.createObjectURL(f) });
      }
      return next;
    });
  }, []);

  const removeShot = (i: number) => {
    setShots(prev => {
      URL.revokeObjectURL(prev[i]?.preview);
      return prev.filter((_, j) => j !== i);
    });
  };

  const onPaste = (e: React.ClipboardEvent) => {
    const files = Array.from(e.clipboardData?.files ?? []).filter(f => f.type.startsWith("image/"));
    if (files.length) { e.preventDefault(); addShots(files); }
  };

  const context = useCallback((): Record<string, unknown> => {
    const lastQ = thread?.userHistory?.filter(m => m.role === "user").slice(-1)[0];
    const lastTurn = thread?.turns?.slice(-1)[0];
    return {
      tab,
      thread_id: thread?.id ?? null,
      thread_title: thread?.title ?? null,
      last_question: typeof lastQ?.content === "string" ? lastQ.content.slice(0, 500) : null,
      last_answer_head: lastTurn?.answer ? lastTurn.answer.slice(0, 1500) : null,
      doc_id: viewerDocId,
      selected_doc_ids: selectedDocIds,
      ua: navigator.userAgent,
      ...(extraCtx.current || {}),
    };
  }, [tab, thread, viewerDocId, selectedDocIds]);

  const submit = async () => {
    if (!message.trim() || state === "sending") return;
    setState("sending");
    try {
      const names = shots.length
        ? await uploadFeedbackImages(shots.map(s => s.file))
        : [];
      await postFeedback({
        category, message: message.trim(), context: context(),
        ...(names.length ? { attachments: names } : {}),
      });
      setState("sent");
      setMessage("");
      shots.forEach(s => URL.revokeObjectURL(s.preview));
      setShots([]);
      extraCtx.current = null;
      getMyFeedback().then(setMine).catch(() => {});
      window.setTimeout(() => { setOpen(false); setState("idle"); }, 1400);
    } catch {
      setState("error");
    }
  };

  return (
    <div className="fb">
      {open && (
        <div className="fb__panel" role="dialog" aria-label="Send feedback">
          <div className="fb__head">
            <span>Send feedback</span>
            <button className="fb__close" onClick={() => setOpen(false)} aria-label="Close">×</button>
          </div>
          <div className="fb__cats" role="radiogroup" aria-label="Category">
            {CATEGORIES.map(c => (
              <button
                key={c.key}
                className={`fb__cat${category === c.key ? " fb__cat--on" : ""}`}
                onClick={() => setCategory(c.key)}
                type="button"
              >
                {c.label}
              </button>
            ))}
          </div>
          <textarea
            className="fb__text"
            placeholder={
              category === "extraction"
                ? "Which document / field is wrong or missing?"
                : category === "answer"
                  ? "What did it get wrong?"
                  : "What's buggy or confusing?"
            }
            value={message}
            onChange={e => setMessage(e.target.value)}
            onPaste={onPaste}
            rows={4}
            maxLength={4000}
          />
          {shots.length > 0 && (
            <div className="fb__shots">
              {shots.map((s, i) => (
                <span key={s.preview} className="fb__shot">
                  <img src={s.preview} alt={`screenshot ${i + 1}`} />
                  <button type="button" aria-label="Remove screenshot" onClick={() => removeShot(i)}>×</button>
                </span>
              ))}
            </div>
          )}
          {shotErr && <div className="fb__err">{shotErr}</div>}
          <div className="fb__foot">
            <button
              className="fb__attach"
              type="button"
              title="Attach up to 3 screenshots (or paste one into the text box)"
              disabled={shots.length >= MAX_SHOTS}
              onClick={() => shotRef.current?.click()}
            >
              📎 Screenshot
            </button>
            <input
              ref={shotRef} type="file" accept="image/*" multiple style={{ display: "none" }}
              onChange={e => { addShots(Array.from(e.target.files ?? [])); if (shotRef.current) shotRef.current.value = ""; }}
            />
            <span className="fb__note">Attaches: your account, current tab, active chat.</span>
            <button
              className="fb__send"
              onClick={submit}
              disabled={!message.trim() || state === "sending"}
              type="button"
            >
              {state === "sending" ? "Sending…" : "Send"}
            </button>
          </div>
          {state === "sent" && <div className="fb__ok">Thanks, logged for the developer.</div>}
          {state === "error" && <div className="fb__err">Couldn't send. Try again.</div>}
          {mine.length > 0 && (
            <div className="fb__mine">
              <div className="fb__mine-h">Your reports</div>
              {mine.slice(0, 5).map(it => (
                <div key={it.id} className="fb__mine-row" title={it.message}>
                  <span className={`fb__mine-status fb__mine-status--${it.status}`}>
                    {it.status === "acknowledged" ? "seen" : it.status}
                  </span>
                  <span className="fb__mine-msg">{it.message}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      <button
        className="fb__fab"
        onClick={() => { setState("idle"); setOpen(o => !o); }}
        type="button"
        title="Report a problem or suggest an improvement"
      >
        Feedback
      </button>
    </div>
  );
}
