import { useEffect, useRef, useState } from "react";
import { AdminJob } from "../api";
import { useAdminJobs } from "../hooks/useAdminJobs";
import { DocumentMeta } from "../types";
import { cleanTitle } from "../docmeta";
import "./JobTicker.css";

// Thin app-level strip above the content: shows the running extraction or
// knowledge-refresh job (or a failed one) on every tab, not just Admin.
// Clicking it opens Admin > Documents. A running job that finishes raises a
// corner toast with a one-click jump into Review for that document.

interface Props {
  docs: DocumentMeta[];
  onOpenAdminDocs: () => void;
  onReviewDoc: (docId: string) => void;
}

export function JobTicker({ docs, onOpenAdminDocs, onReviewDoc }: Props) {
  const { jobs } = useAdminJobs();
  // The dismissal remembers WHICH jobs (id + status) it hid, so any new job
  // or status change makes the strip reappear.
  const [dismissedSig, setDismissedSig] = useState<string | null>(null);
  const [toast, setToast] = useState<{ docId: string; kind?: string } | null>(null);
  const toastTimer = useRef<number | undefined>(undefined);
  const prev = useRef<Record<string, AdminJob>>({});

  const title = (id: string) => {
    const d = docs.find(x => x.doc_id === id);
    return d ? cleanTitle(d.title) : id.slice(0, 12);
  };

  // running → done transition = the completion toast.
  useEffect(() => {
    const before = prev.current;
    prev.current = jobs;
    const doneId = Object.keys(jobs).find(
      k => before[k]?.status === "running" && jobs[k].status === "done");
    if (doneId) {
      setToast({ docId: doneId, kind: jobs[doneId].kind });
      window.clearTimeout(toastTimer.current);
      toastTimer.current = window.setTimeout(() => setToast(null), 12000);
    }
  }, [jobs]);

  const active = Object.entries(jobs)
    .filter(([, j]) => j.status === "running" || j.status === "error");
  const running = active.filter(([, j]) => j.status === "running");
  const sig = active.map(([id, j]) => `${id}:${j.status}`).sort().join("|");

  // Everything settled: forget the dismissal so the next job shows again
  // even if it lands with an identical signature.
  useEffect(() => {
    if (active.length === 0) setDismissedSig(null);
  }, [active.length]);

  const showStrip = active.length > 0 && sig !== dismissedSig;

  let text = "";
  let isErr = false;
  if (showStrip) {
    if (running.length > 0) {
      const [id, j] = running[0];
      const verb = j.kind === "refresh" ? "Refreshing" : "Extracting";
      const extra = active.length > 1 ? ` · ${active.length - 1} more` : "";
      text = `${verb} ${title(id)}: ${j.stage ?? "working…"}${extra}`;
    } else {
      const [id, j] = active[0];
      isErr = true;
      text = `${title(id)} failed: ${(j.error ?? "unknown error").slice(0, 140)}`;
    }
  }

  return (
    <>
      {showStrip && (
        <div
          className={`jobticker${isErr ? " jobticker--err" : ""}`}
          role="status"
          title="Open Admin > Documents"
          onClick={onOpenAdminDocs}
        >
          <span className={`jobticker__dot${isErr ? " jobticker__dot--err" : ""}`} aria-hidden />
          <span className="jobticker__text">{text}</span>
          <button
            className="jobticker__x"
            aria-label="Dismiss"
            onClick={e => { e.stopPropagation(); setDismissedSig(sig); }}
          >✕</button>
        </div>
      )}
      {toast && (
        <div className="toast" role="status">
          <span className="toast__text">
            {toast.kind === "refresh"
              ? `"${title(toast.docId)}" finished its knowledge refresh.`
              : `"${title(toast.docId)}" is extracted and ready.`}
            {" "}
            <button
              className="jobticker__go"
              onClick={() => { onReviewDoc(toast.docId); setToast(null); }}
            >
              Review it now
            </button>
          </span>
          <button className="toast__x" aria-label="Dismiss" onClick={() => setToast(null)}>✕</button>
        </div>
      )}
    </>
  );
}
