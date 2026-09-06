import { useEffect, useMemo, useState } from "react";
import {
  ReviewBlock, ReviewDocRecord, ReviewDocSummary, ReviewEvidence, ReviewField,
  ReviewSearchMatch, ReviewVote, attachReviewEvidence, getReviewBlocks,
  getReviewDoc, listDocuments, listReviewDocs, rejectReviewEvidence,
  reviewPageUrl, searchReviewDoc, voteReviewField,
} from "../api";
import { useAuth } from "../auth";
import { AuthedImage } from "./AuthedImage";
import { ReviewDocList } from "./ReviewDocList";
import { ReviewHeatmap } from "./ReviewHeatmap";
import { cleanTitle } from "../docmeta";
import { PageJump } from "./PageJump";
import { loadUi, saveUi } from "../uiState";
import "./ReviewView.css";
import { useDocNoun } from "../branding";

// The human-verification review screen: a contract page on the left with its
// evidence highlighted, its extracted fields (grouped by category, with
// green/yellow/red status + clustered multi-evidence) on the right. Click a
// field → its bbox highlights on the page; for multi-value fields ONE value is
// focused (bright) at a time and the rest stay faint, so a 40-row equipment
// table never becomes an unreadable blob. Verification is a VOTE (GitHub-review
// style): every approved verifier can approve the value, reject it, or propose
// a correction (optionally anchored to its clause); a correction needs a second
// verifier to concur before it takes effect. Fields are clearance-filtered
// server-side.

interface Rect { page_no: number; bbox: [number, number, number, number]; }

function parseRects(ev: ReviewEvidence): Rect[] {
  if (!ev.rects) return [];
  try {
    const arr = JSON.parse(ev.rects);
    return Array.isArray(arr) ? (arr as Rect[]) : [];
  } catch { return []; }
}

function parseRectsJson(rects: string | null | undefined): Rect[] {
  if (!rects) return [];
  try {
    const arr = JSON.parse(rects);
    return Array.isArray(arr) ? (arr as Rect[]) : [];
  } catch { return []; }
}

/** The page that best tells one value apart from its siblings. Multi-value
 * fields anchor every value to a shared generic clause PLUS its own table row,
 * and the shared clause's rect comes first — so "jump to the first rect" lands
 * every pill on the same boilerplate page. Rank this value's pages by how few
 * sibling values also anchor there: the page only this value cites (its row)
 * wins over the page all of them cite. */
function distinctivePage(field: ReviewField, pages: number[]): number | null {
  if (pages.length === 0) return null;
  const freq = new Map<number, number>();
  for (const v of field.values) {
    for (const p of new Set(v.evidence.flatMap(parseRects).map(r => r.page_no))) {
      freq.set(p, (freq.get(p) ?? 0) + 1);
    }
  }
  return [...pages].sort(
    (a, b) => (freq.get(a) ?? 0) - (freq.get(b) ?? 0) || a - b)[0];
}

/** Distinct rect pages of one value's evidence. */
function valuePages(v: { evidence: ReviewEvidence[] } | undefined): number[] {
  return [...new Set((v?.evidence ?? []).flatMap(parseRects).map(r => r.page_no))];
}

const STATUS: Record<string, string> = {
  green: "#4dd4ac", yellow: "#f59e0b", red: "#f87171", grey: "#8a90a0",
};

/** Short display name for a vote chip: first name, or the email's user part. */
const voterLabel = (v: ReviewVote): string =>
  (v.voter_name || v.voter).split("@")[0].split(" ")[0];

/** The distinct corrections verifiers proposed (approve votes carrying a value),
 * with their vote counts — rendered as "concur?" rows, GitHub-suggestion style. */
function pendingAmendments(f: ReviewField): { value: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const v of f.votes ?? []) {
    if (v.decision === "approve" && v.value) {
      counts.set(v.value, (counts.get(v.value) ?? 0) + 1);
    }
  }
  return [...counts.entries()].map(([value, count]) => ({ value, count }));
}

/** First field (in display order) that has a highlightable clause + its page —
 * so opening a document immediately shows an evidence highlight, rather than
 * landing on a cover page with nothing lit up. */
function firstEvidenced(rec: ReviewDocRecord): { key: string; page: number } | null {
  for (const [key, f] of Object.entries(rec.fields)) {
    for (const v of f.values) {
      for (const ev of v.evidence) {
        const r = parseRects(ev)[0];
        if (r) return { key, page: r.page_no };
      }
    }
  }
  return null;
}

/** Every field key in display order: grouped by category in first-appearance
 * order, exactly how byCategory renders them. Shared by the queue derivation
 * and the post-vote advance so keyboard order always matches the screen. */
function orderedKeys(rec: ReviewDocRecord): string[] {
  const cats: Record<string, string[]> = {};
  for (const [k, f] of Object.entries(rec.fields)) (cats[f.category] ??= []).push(k);
  return Object.values(cats).flat();
}

/** Scroll a field row into view once the selection paints. */
function scrollToField(key: string) {
  requestAnimationFrame(() =>
    document.querySelector(`[data-fieldkey="${CSS.escape(key)}"]`)
      ?.scrollIntoView({ block: "nearest" }));
}

export function ReviewView() {
  const noun = useDocNoun();
  const { user } = useAuth();
  const myEmail = user?.email ?? "";
  // Mode and document survive tab switches (and admin's "Review" links land
  // here) via the session-scoped ui hints.
  const [mode, setMode] = useState<"doc" | "overview">(() => loadUi("review.mode", "overview"));
  // Overview flavour: the document list is the default ("which documents can I
  // trust?"); the field×category heatmap stays available for per-category detail.
  const [ovKind, setOvKind] = useState<"list" | "grid">("list");
  const [docs, setDocs] = useState<ReviewDocSummary[]>([]);
  const [docId, setDocId] = useState<string | null>(() => loadUi<string | null>("review.docId", null));
  // Triage filters: click a legend chip to see only that status, and hide
  // already-verified fields to walk the remaining queue without hunting.
  const [statusFilter, setStatusFilter] = useState<string | null>(null);
  const [hideVerified, setHideVerified] = useState(false);
  // Queue mode: walk the unverified populated fields one by one with keyboard
  // shortcuts (A approve, R reject, E correct). The queue itself is derived
  // fresh each render, so leaving and re-entering always starts clean.
  const [queueMode, setQueueMode] = useState(false);
  const [rec, setRec] = useState<ReviewDocRecord | null>(null);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  // Which VALUE of the active field is focused: its rects render bright, the
  // rest render faint. Click a value chip, a page highlight, or the stepper.
  const [activeValueIdx, setActiveValueIdx] = useState(0);
  const [page, setPage] = useState<number>(1);
  // Phones show one deliberate task at a time: choose a field, then inspect
  // its source page. Desktop keeps both panes visible through CSS.
  const [mobilePane, setMobilePane] = useState<"fields" | "page">("fields");
  const [busy, setBusy] = useState<string | null>(null);
  // Page-block select modes: "Add evidence" attaches a missed clause to any
  // field; the correction editor's "Mark clause" anchors a proposed correction.
  const [addMode, setAddMode] = useState(false);
  const [corrPick, setCorrPick] = useState(false);
  const [blocks, setBlocks] = useState<ReviewBlock[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const [attachField, setAttachField] = useState<string>("");
  const [attachValue, setAttachValue] = useState<string>("");
  // Inline value editor (replaces the old browser prompt): the whole value is
  // visible and editable in place, next to its evidence.
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState<string>("");

  // Page count per document — bounds the pager ("page 3 of 47", no walking
  // past the last page into a broken image).
  const [pageCounts, setPageCounts] = useState<Record<string, number>>({});

  // Find-in-document over the OCR blocks (server-side): jump to the page and
  // flash the matched clause. Saves the scroll-hunt when anchoring a correction.
  const [searchQ, setSearchQ] = useState("");
  const [searchHits, setSearchHits] = useState<ReviewSearchMatch[] | null>(null);
  const [searchIdx, setSearchIdx] = useState(0);
  const [searchBusy, setSearchBusy] = useState(false);

  const doSearch = async () => {
    if (!docId || searchQ.trim().length < 2) { setSearchHits(null); return; }
    setSearchBusy(true);
    try {
      const ms = await searchReviewDoc(docId, searchQ.trim());
      setSearchHits(ms);
      setSearchIdx(0);
      if (ms.length) setPage(ms[0].page);
    } finally {
      setSearchBusy(false);
    }
  };
  const gotoHit = (delta: number) => {
    if (!searchHits || searchHits.length === 0) return;
    const next = (searchIdx + delta + searchHits.length) % searchHits.length;
    setSearchIdx(next);
    setPage(searchHits[next].page);
  };

  useEffect(() => {
    listReviewDocs()
      .then(d => {
        setDocs(d);
        // Keep a restored doc only if it still exists, else fall back.
        setDocId(prev => (prev && d.some(x => x.doc_id === prev)) ? prev : d[0]?.doc_id ?? null);
      })
      .catch(e => console.error("listReviewDocs:", e));
    listDocuments()
      .then(all => setPageCounts(
        Object.fromEntries(all.map(d => [d.doc_id, d.total_pages || 0]))))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (!docId) { setRec(null); return; }
    getReviewDoc(docId)
      .then(r => {
        setRec(r);
        const first = firstEvidenced(r);   // land on a lit-up field, not the cover
        setActiveKey(first?.key ?? null);
        setActiveValueIdx(0);
        setPage(first?.page ?? 1);
      })
      .catch(e => console.error("getReviewDoc:", e));
    setQueueMode(false);                   // a new document starts outside the queue
    setSearchQ(""); setSearchHits(null); setSearchIdx(0);
  }, [docId]);

  // The queue only makes sense inside a document.
  useEffect(() => { if (mode !== "doc") setQueueMode(false); }, [mode]);

  // Remember the reviewer's place across tab switches.
  useEffect(() => { saveUi("review.mode", mode); }, [mode]);
  useEffect(() => { if (docId) saveUi("review.docId", docId); }, [docId]);

  const picking = addMode || corrPick;

  // Selectable page blocks (fetched only while a select mode is on; per page).
  useEffect(() => {
    if (!picking || !docId) { setBlocks([]); setSelected([]); return; }
    getReviewBlocks(docId, page).then(setBlocks).catch(() => setBlocks([]));
    setSelected([]);
  }, [picking, docId, page]);

  function toggleBlock(i: number) {
    setSelected(prev => {
      const next = prev.includes(i) ? prev.filter(x => x !== i) : [...prev, i];
      if (addMode) {
        // Prefill the attach value with the first selected clause's text (trimmed).
        const first = next.length ? blocks[next[0]]?.text ?? "" : "";
        setAttachValue(v => (v && prev.length ? v : first.slice(0, 120)));
        if (!next.length) setAttachValue("");
      }
      return next;
    });
  }

  /** The evidence blob of the currently selected blocks (correction anchor). */
  function selectedEvidence(): { snippet: string; page: number; rects: string } | undefined {
    const chosen = selected.map(i => blocks[i]).filter(Boolean);
    if (!chosen.length) return undefined;
    return {
      snippet: chosen.map(b => b.text).join(" ").slice(0, 240),
      page,
      rects: JSON.stringify(chosen.map(b => ({ page_no: page, bbox: b.bbox }))),
    };
  }

  async function doAttach() {
    if (!docId || !attachField || !attachValue.trim() || selected.length === 0) return;
    setBusy("attach");
    try {
      const chosen = selected.map(i => blocks[i]).filter(Boolean);
      const snippet = chosen.map(b => b.text).join(" ").slice(0, 240);
      const rects = JSON.stringify(chosen.map(b => ({ page_no: page, bbox: b.bbox })));
      await attachReviewEvidence(docId, attachField, {
        value: attachValue.trim(), snippet, page, rects,
      });
      setAddMode(false); setSelected([]); setAttachValue("");
      setRec(await getReviewDoc(docId));
      setActiveKey(attachField);
      setActiveValueIdx(0);
    } catch (e) { console.error("attach:", e); }
    setBusy(null);
  }

  async function doRejectEvidence(fieldKey: string, ev: ReviewEvidence, value: string) {
    if (!docId) return;
    if (!window.confirm("Remove this clause from the field? Use this when the highlight points at the wrong text.")) return;
    setBusy(fieldKey);
    try {
      // rects + owning value make the rejection precise: only this rect-twin
      // detaches, and only from the value it was clicked on.
      await rejectReviewEvidence(docId, fieldKey, ev.page, ev.snippet, ev.rects, value);
      setRec(await getReviewDoc(docId));
    } catch (e) { console.error("reject evidence:", e); }
    setBusy(null);
  }

  const activeField = activeKey && rec ? rec.fields[activeKey] : null;
  const nValues = activeField?.values.length ?? 0;
  const focusIdx = nValues > 0 ? Math.min(activeValueIdx, nValues - 1) : 0;
  const cq = rec?.correction_quorum ?? 2;

  const highlights = useMemo(() => {
    if (!activeField) {
      return [] as { bbox: [number, number, number, number]; key: string; vi: number; label: string; cls: string }[];
    }
    const out: { bbox: [number, number, number, number]; key: string; vi: number; label: string; cls: string }[] = [];
    // When a correction won WITH its own clause, the machine rects all demote:
    // the green corrected anchor is the one that backs the value people see.
    const corrected = !!(activeField.verified && activeField.verified_value
                         && activeField.verified_evidence?.rects);
    activeField.values.forEach((v, vi) =>
      v.evidence.forEach((ev, ei) =>
        parseRects(ev).filter(r => r.page_no === page).forEach((r, ri) =>
          out.push({
            bbox: r.bbox, key: `${vi}-${ei}-${ri}`, vi, label: String(v.value),
            cls: corrected || vi !== focusIdx ? " review__hl--dim" : "",
          }))));
    parseRectsJson(activeField.verified_evidence?.rects)
      .filter(r => r.page_no === page)
      .forEach((r, i) => out.push({
        bbox: r.bbox, key: `corr-${i}`, vi: -1,
        label: activeField.verified_value
          ? `Corrected: ${activeField.verified_value}` : "Corrected value",
        cls: " review__hl--corr",
      }));
    return out;
  }, [activeField, page, focusIdx]);

  function selectField(key: string, r: ReviewDocRecord | null = rec) {
    setActiveKey(key);
    setActiveValueIdx(0);
    setEditingKey(null); setCorrPick(false);
    setMobilePane("page");
    const f = r?.fields[key];
    const v0 = f?.values[0];
    if (!f || !v0) return;
    const best = distinctivePage(f, valuePages(v0))
      ?? v0.evidence.find(e => e.page != null)?.page;
    if (best != null) setPage(best);
  }

  /** Focus one value of the active field and jump the page to the clause that
   * distinguishes it (its own table row, not the clause every value shares). */
  function focusValue(vi: number) {
    setActiveValueIdx(vi);
    const v = activeField?.values[vi];
    if (!activeField || !v) return;
    const best = distinctivePage(activeField, valuePages(v))
      ?? v.evidence.find(e => e.page != null)?.page;
    if (best != null) setPage(best);
  }

  async function doVote(key: string, decision: "approve" | "reject", value?: string,
                        ev?: { snippet: string; page: number; rects: string }) {
    if (!docId) return;
    setBusy(key);
    try {
      await voteReviewField(docId, key, { decision, value, ...(ev ?? {}) });
      const fresh = await getReviewDoc(docId);
      setRec(fresh);
      if (queueMode) advanceAfterVote(key, fresh);
    } catch (e) { console.error("vote:", e); }
    setBusy(null);
  }

  const byCategory = useMemo(() => {
    const m: Record<string, [string, ReviewField][]> = {};
    if (rec) for (const [k, f] of Object.entries(rec.fields)) (m[f.category] ??= []).push([k, f]);
    return m;
  }, [rec]);

  const fieldVisible = (f: ReviewField): boolean =>
    (!statusFilter || f.status === statusFilter) && (!hideVerified || !f.verified);

  // Fields in display order after the triage filters. This is what the arrow
  // keys walk, so keyboard order always matches what is on screen.
  const visibleKeys = useMemo(() => {
    const out: string[] = [];
    for (const fields of Object.values(byCategory)) {
      for (const [key, f] of fields) if (fieldVisible(f)) out.push(key);
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [byCategory, statusFilter, hideVerified]);

  // How many populated fields already carry a verification.
  const nVerifiedFields = useMemo(
    () => (rec ? Object.values(rec.fields).filter(f => f.verified).length : 0),
    [rec]);

  // The verification queue: visible fields (so an active triage filter is
  // honored) that carry values and are not yet verified, in display order.
  const queueKeys = useMemo(
    () => rec
      ? orderedKeys(rec).filter(k => {
          const f = rec.fields[k];
          return fieldVisible(f) && f.values.length > 0 && !f.verified;
        })
      : [],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rec, statusFilter, hideVerified]);
  const queuePos = activeKey ? queueKeys.indexOf(activeKey) : -1;

  function startQueue() {
    if (queueKeys.length === 0) return;
    setAddMode(false); setSelected([]); setAttachValue("");
    setQueueMode(true);
    // Keep the current field when it is already in the queue, else start at 1.
    const first = activeKey && queueKeys.includes(activeKey) ? activeKey : queueKeys[0];
    selectField(first);
    scrollToField(first);
  }

  /** Step to the next/previous unverified field in the queue (wraps). */
  function queueStep(dir: 1 | -1) {
    if (queueKeys.length === 0) return;
    const i = activeKey ? queueKeys.indexOf(activeKey) : -1;
    const next = i === -1
      ? (dir === 1 ? queueKeys[0] : queueKeys[queueKeys.length - 1])
      : queueKeys[(i + (dir === 1 ? 1 : queueKeys.length - 1)) % queueKeys.length];
    selectField(next);
    scrollToField(next);
  }

  /** After a vote resolves, move to the next still-unverified field. Derived
   * from the FRESH record so fields the vote just verified are skipped. */
  function advanceAfterVote(votedKey: string, fresh: ReviewDocRecord) {
    const order = orderedKeys(fresh);
    const q = order.filter(k => {
      const f = fresh.fields[k];
      return fieldVisible(f) && f.values.length > 0 && !f.verified;
    });
    if (q.length === 0) { setActiveKey(null); return; }   // queue done
    const pos = order.indexOf(votedKey);
    const next = q.find(k => order.indexOf(k) > pos) ?? q[0];
    selectField(next, fresh);
    scrollToField(next);
  }

  // Keyboard path for the verifier: up/down (or j/k) walk the visible fields,
  // left/right step through a multi-value field, Escape backs out of the
  // add-evidence and correction modes (then queue mode). In queue mode the
  // list keys walk the QUEUE instead, and A/R/E act on the active field.
  // Typing in any input is left alone.
  useEffect(() => {
    if (mode !== "doc") return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      const typing = !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA"
        || t.tagName === "SELECT" || t.isContentEditable);
      if (e.key === "Escape") {
        if (corrPick) { setCorrPick(false); setSelected([]); }
        else if (editingKey) { setEditingKey(null); setSelected([]); }
        else if (addMode) { setAddMode(false); setSelected([]); setAttachValue(""); }
        else if (queueMode) setQueueMode(false);
        return;
      }
      if (typing) return;
      if ((e.key === "ArrowRight" || e.key === "ArrowLeft") && activeField && nValues > 1) {
        e.preventDefault();
        focusValue((focusIdx + (e.key === "ArrowRight" ? 1 : nValues - 1)) % nValues);
        return;
      }
      if (queueMode) {
        if (busy) return;                       // a vote is in flight
        const k = e.key.toLowerCase();
        const fwd = k === "n" || k === "j" || e.key === "ArrowDown";
        const back = k === "p" || k === "k" || e.key === "ArrowUp";
        if (fwd || back) { e.preventDefault(); queueStep(fwd ? 1 : -1); return; }
        const f = activeKey ? rec?.fields[activeKey] : null;
        if (!f || !f.can_edit || !activeKey) return;
        // Mirror the action buttons' disabled states so a repeat keypress
        // cannot re-send a vote the buttons would refuse.
        if (k === "a" && !(f.my_vote?.decision === "approve" && !f.my_vote?.value)) {
          e.preventDefault();
          doVote(activeKey, "approve");
        } else if (k === "r" && f.my_vote?.decision !== "reject") {
          e.preventDefault();
          doVote(activeKey, "reject");
        } else if (k === "e") {
          e.preventDefault();
          setEditingKey(activeKey);
          setEditValue(String(f.my_vote?.value ?? f.verified_value ?? f.values[0]?.value ?? ""));
        }
        return;
      }
      const down = e.key === "ArrowDown" || e.key === "j";
      const up = e.key === "ArrowUp" || e.key === "k";
      if (down || up) {
        if (visibleKeys.length === 0) return;
        e.preventDefault();
        const idx = activeKey ? visibleKeys.indexOf(activeKey) : -1;
        const next = idx === -1
          ? (down ? visibleKeys[0] : visibleKeys[visibleKeys.length - 1])
          : visibleKeys[(idx + (down ? 1 : visibleKeys.length - 1)) % visibleKeys.length];
        selectField(next);
        scrollToField(next);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, visibleKeys, queueKeys, activeKey, activeField, focusIdx, nValues,
      corrPick, editingKey, addMode, queueMode, busy, rec]);

  // How many visible fields actually carry a value. A low ratio isn't a data
  // loss — amendments/letters restate only what they change; the rest inherit
  // from the base contract. Surface that so an all-"not stated" doc reads as
  // expected, not broken.
  const populated = useMemo(
    () => (rec ? Object.values(rec.fields).filter(f => f.values.length > 0).length : 0),
    [rec]);
  const total = rec ? Object.keys(rec.fields).length : 0;
  const sparse = rec != null && total > 0 && populated / total < 0.25;

  return (
    <div className={`review${mode === "doc" ? ` review--mobile-${mobilePane}` : ""}`}>
      <div className="review__bar">
        <div className="review__modes">
          <button className={mode === "overview" ? "is-active" : ""} onClick={() => setMode("overview")}>Overview</button>
          <button className={mode === "doc" ? "is-active" : ""} onClick={() => setMode("doc")}>Review a document</button>
        </div>
        {mode === "overview" && (
          <div className="review__modes">
            <button className={ovKind === "list" ? "is-active" : ""} onClick={() => setOvKind("list")}>By document</button>
            <button className={ovKind === "grid" ? "is-active" : ""} onClick={() => setOvKind("grid")}>By category</button>
          </div>
        )}
        {mode === "doc" && (
          <select className="review__doc" value={docId ?? ""} onChange={e => setDocId(e.target.value)}>
            {docs.map(d => (
              <option key={d.doc_id} value={d.doc_id}>
                {cleanTitle(d.title)} ({d.n_populated} values)
              </option>
            ))}
          </select>
        )}
        {mode === "doc" && rec?.can_edit && !queueMode && (
          <button
            className={`review__addev${addMode ? " is-on" : ""}`}
            onClick={() => { setAddMode(m => !m); setCorrPick(false); }}
            title="Click clauses on the page that the AI missed, then attach them to a field"
          >
            {addMode ? "✕ Cancel" : "＋ Add evidence"}
          </button>
        )}
        {mode === "doc" && rec?.can_edit && !queueMode && queueKeys.length > 0 && (
          <button
            className="review__queue-start"
            onClick={startQueue}
            title="Walk every unverified field one by one with keyboard shortcuts"
          >
            ▶ Start reviewing
          </button>
        )}
        {mode === "doc" && queueMode && (
          <span className="review__queue">
            {queueKeys.length === 0 ? (
              <span>
                <b>Every field reviewed</b> · {nVerifiedFields}/{populated} verified
              </span>
            ) : (
              <span>
                Reviewing <b>{queuePos >= 0 ? `${queuePos + 1} of ` : ""}{queueKeys.length}</b>
                {queuePos < 0 ? " left" : ""}
              </span>
            )}
            {queueKeys.length > 0 && (
              <span className="review__queue-keys">
                <kbd>A</kbd> approve <kbd>R</kbd> reject <kbd>E</kbd> correct
                {" "}<kbd>↑</kbd><kbd>↓</kbd> move <kbd>Esc</kbd> exit
              </span>
            )}
            <button className="review__queue-exit" onClick={() => setQueueMode(false)}>
              Exit
            </button>
          </span>
        )}
        {mode === "doc" && rec && (
          <span className="review__counts" title="How the AI rated its own extractions. Click a chip to show only those fields.">
            {([
              ["green", "confident"], ["yellow", "to check"],
              ["red", "needs attention"], ["grey", "blank"],
            ] as const).map(([st, label]) => (
              <button
                key={st}
                type="button"
                className={`review__count${statusFilter === st ? " is-on" : ""}`}
                onClick={() => setStatusFilter(s => (s === st ? null : st))}
                title={statusFilter === st ? "Show all fields again" : `Show only "${label}" fields`}
              >
                <b style={{ color: STATUS[st] }}>●</b> {rec.status_counts[st] ?? 0} {label}
              </button>
            ))}
            <button
              type="button"
              className={`review__count${hideVerified ? " is-on" : ""}`}
              onClick={() => setHideVerified(v => !v)}
              title="Hide fields that already carry a verification, leaving the remaining queue"
            >
              ✓ hide verified
            </button>
            <span className="review__progress" title="Fields verified so far, out of the fields this document states">
              <b>{nVerifiedFields}</b>/{populated} verified
            </span>
            {rec.hidden_fields > 0 && <em> · {rec.hidden_fields} hidden</em>}
          </span>
        )}
        {mode === "doc" && (
          <div className="review__mobile-pane" role="tablist" aria-label="Review workspace">
            <button
              type="button"
              role="tab"
              aria-selected={mobilePane === "fields"}
              className={mobilePane === "fields" ? "is-active" : ""}
              onClick={() => setMobilePane("fields")}
            >Fields</button>
            <button
              type="button"
              role="tab"
              aria-selected={mobilePane === "page"}
              className={mobilePane === "page" ? "is-active" : ""}
              onClick={() => setMobilePane("page")}
            >Source page</button>
          </div>
        )}
      </div>

      {mode === "doc" && rec?.can_edit && !queueMode && (
        <div className="review__kbdhint">
          Keyboard: <kbd>↑</kbd><kbd>↓</kbd> walk fields · <kbd>←</kbd><kbd>→</kbd> step values
          {queueKeys.length > 0 && (
            <> · or press <b>▶ Start reviewing</b> for one-key <kbd>A</kbd> approve
            {" "}/ <kbd>R</kbd> reject / <kbd>E</kbd> correct with auto-advance</>
          )}
        </div>
      )}

      {mode === "overview" ? (
        ovKind === "list"
          ? <ReviewDocList onPick={id => { setDocId(id); setMode("doc"); setMobilePane("fields"); }} />
          : <ReviewHeatmap onPick={id => { setDocId(id); setMode("doc"); setMobilePane("fields"); }} />
      ) : (
      <div className="review__body">
        <div className="review__page">
          {docId && (
            <div className="review__canvas">
              <div className="review__imgwrap">
                <AuthedImage src={reviewPageUrl(docId, page)} alt={`page ${page}`} />
                {!picking && highlights.map(h => {
                  const [x0, y0, x1, y1] = h.bbox;
                  return (
                    <div
                      key={h.key}
                      className={`review__hl${h.cls}`}
                      title={h.label}
                      style={{
                        left: `${x0 * 100}%`, top: `${y0 * 100}%`,
                        width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`,
                      }}
                      onClick={h.vi >= 0 ? () => focusValue(h.vi) : undefined}
                    />
                  );
                })}
                {picking && blocks.map((b, i) => {
                  const [x0, y0, x1, y1] = b.bbox;
                  const on = selected.includes(i);
                  return (
                    <div
                      key={i}
                      className={`review__blk${on ? " is-sel" : ""}`}
                      style={{
                        left: `${x0 * 100}%`, top: `${y0 * 100}%`,
                        width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`,
                      }}
                      title={b.text.slice(0, 160)}
                      onClick={() => toggleBlock(i)}
                    />
                  );
                })}
                {(searchHits ?? [])
                  .map((h, i) => ({ ...h, i }))
                  .filter(h => h.page === page)
                  .map(h => {
                    const [x0, y0, x1, y1] = h.bbox;
                    return (
                      <div
                        key={`search-${h.i}`}
                        className={`review__searchhl${h.i === searchIdx ? " is-active" : ""}`}
                        title={h.snippet}
                        style={{
                          left: `${x0 * 100}%`, top: `${y0 * 100}%`,
                          width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`,
                        }}
                      />
                    );
                  })}
              </div>
            </div>
          )}
          <div className="review__pager">
            <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page <= 1}>◄</button>
            <span>
              page <PageJump page={page} total={docId ? pageCounts[docId] ?? 0 : 0} onJump={setPage} />
              {docId && pageCounts[docId] ? ` of ${pageCounts[docId]}` : ""}
            </span>
            <button
              onClick={() => setPage(p => (docId && pageCounts[docId] ? Math.min(pageCounts[docId], p + 1) : p + 1))}
              disabled={!!docId && !!pageCounts[docId] && page >= pageCounts[docId]}
            >►</button>
            <span className="review__search">
              <input
                className="review__search-input"
                placeholder="Find text…"
                value={searchQ}
                onChange={e => setSearchQ(e.target.value)}
                onKeyDown={e => {
                  e.stopPropagation();     /* the field-walk keys must not fire while typing */
                  if (e.key === "Enter") { e.preventDefault(); searchHits ? gotoHit(1) : doSearch(); }
                  if (e.key === "Escape") { setSearchQ(""); setSearchHits(null); }
                }}
              />
              {searchBusy ? (
                <em>searching…</em>
              ) : searchHits ? (
                <>
                  <em>{searchHits.length === 0 ? "no matches" : `${searchIdx + 1}/${searchHits.length}`}</em>
                  <button onClick={() => gotoHit(-1)} disabled={searchHits.length === 0} aria-label="Previous match">◄</button>
                  <button onClick={() => gotoHit(1)} disabled={searchHits.length === 0} aria-label="Next match">►</button>
                  <button onClick={() => { setSearchQ(""); setSearchHits(null); }} aria-label="Clear search">×</button>
                </>
              ) : null}
            </span>
          </div>

          {addMode && (
            <div className="review__attach">
              {selected.length === 0 ? (
                <div className="review__attach-hint">
                  Click the clause(s) on the page that the AI missed. You can select more than one.
                </div>
              ) : (
                <>
                  <div className="review__attach-snip">
                    "{selected.map(i => blocks[i]?.text ?? "").join(" ").slice(0, 180)}"
                  </div>
                  <div className="review__attach-row">
                    <textarea
                      className="review__attach-value"
                      placeholder="The value this clause states, e.g. 7°C ± 1°C"
                      value={attachValue}
                      rows={Math.min(4, Math.max(1, Math.ceil(attachValue.length / 50)))}
                      onChange={e => setAttachValue(e.target.value)}
                    />
                    <select
                      className="review__attach-field"
                      value={attachField}
                      onChange={e => setAttachField(e.target.value)}
                    >
                      <option value="">Attach to field…</option>
                      {Object.entries(byCategory).map(([cat, fs]) => (
                        <optgroup key={cat} label={cat.replace(/_/g, " ")}>
                          {fs.map(([key, f]) => (
                            <option key={key} value={key}>{f.title}</option>
                          ))}
                        </optgroup>
                      ))}
                    </select>
                    <button
                      className="review__attach-go"
                      disabled={!attachField || !attachValue.trim() || busy === "attach"}
                      onClick={doAttach}
                    >
                      Attach
                    </button>
                  </div>
                  <div className="review__attach-note">
                    Need a field that doesn't exist yet? Add it in the Ontology tab first, then attach here.
                  </div>
                </>
              )}
            </div>
          )}
          {corrPick && (
            <div className="review__attach">
              <div className="review__attach-hint">
                {selected.length === 0
                  ? "Click the clause(s) on the page that state the corrected value, then finish in the editor on the right."
                  : `${selected.length} clause block${selected.length > 1 ? "s" : ""} selected for the correction. Finish in the editor on the right.`}
              </div>
            </div>
          )}
        </div>

        <div className="review__fields">
          {!rec ? (
            docs.length === 0 ? (
              <div className="review__empty">
                Nothing to review yet. Upload a {noun.one} in the Admin tab and it will appear here once extracted.
              </div>
            ) : (
              <div className="skel-wrap" aria-hidden>
                {[64, 44, 44, 64, 44, 44, 64, 44].map((h, i) => (
                  <div key={i} className="skel" style={{ height: h, width: i % 3 === 0 ? "55%" : "100%" }} />
                ))}
              </div>
            )
          ) : Object.keys(rec.fields).length === 0 ? (
            <div className="review__empty">Your access level can't see any fields in this document. Ask an admin if you need access.</div>
          ) : (
            <>
            {sparse && (
              <div className="review__note">
                Only <b>{populated}</b> of {total} fields are stated in this document.
                That's normal for an amendment or letter: it restates only what it
                changes, and everything else <b>carries over from the base {noun.one}</b>.
                A blank here means "unchanged", not missing.
              </div>
            )}
            {visibleKeys.length === 0 && (statusFilter || hideVerified) && (
              <div className="review__empty">
                No fields match the current filter. Click the highlighted chip
                in the bar above to show everything again.
              </div>
            )}
            {Object.entries(byCategory).map(([cat, fields]) => {
              const shown = fields.filter(([, f]) => fieldVisible(f));
              if (shown.length === 0) return null;
              return (
              <div key={cat} className="review__cat">
                <div className="review__cat-h">
                  {cat.replace(/_/g, " ")}
                  {fields[0][1].sensitivity === "confidential" && <span className="review__lock">🔒</span>}
                </div>
                {shown.map(([key, f]) => {
                  const isCorrected = !!(f.verified && f.verified_value);
                  const fv = f.values[activeKey === key ? Math.min(focusIdx, Math.max(0, f.values.length - 1)) : 0];
                  return (
                  <div
                    key={key}
                    data-fieldkey={key}
                    className={`review__field${activeKey === key ? " is-active" : ""}`}
                    onClick={() => selectField(key)}
                  >
                    <span className="review__dot" style={{ background: STATUS[f.status] ?? STATUS.grey }} />
                    <div className="review__field-main">
                      <div className="review__field-title">
                        {f.title}
                        {f.conflict && <span className="review__conflict">⚠ conflict</span>}
                        {f.verified && (
                          <span
                            className="review__ok"
                            title={`Verified by ${(f.verifiers ?? []).join(", ") || "a reviewer"} (largest share of ${f.n_votes ?? 1} vote${(f.n_votes ?? 1) > 1 ? "s" : ""})`}
                          >
                            ✓ {f.confidence != null ? `${Math.round(f.confidence * 100)}%` : "verified"}
                            {(f.n_votes ?? 0) > 0 ? ` (${(f.verifiers ?? []).length}/${f.n_votes})` : ""}
                          </span>
                        )}
                        {f.needs_correction
                          ? <span className="review__conflict">✗ needs correction</span>
                          : f.disputed && <span className="review__conflict">⚠ disputed</span>}
                        {!f.verified && f.pending_value && (
                          <span className="review__ok" title={`Proposed correction "${f.pending_value}" is waiting for another verifier to concur`}>
                            ✎ pending concur
                          </span>
                        )}
                      </div>
                      {isCorrected && activeKey === key && (
                        <div className="review__corr" onClick={e => e.stopPropagation()}>
                          <div className="review__corr-h">
                            ✓ Corrected value · {(f.verifiers ?? []).length} verifier{(f.verifiers ?? []).length > 1 ? "s" : ""}
                          </div>
                          <div className="review__corr-val">{f.verified_value}</div>
                          {f.verified_evidence?.snippet && (
                            <div
                              className="review__ev"
                              onClick={() => {
                                const r = parseRectsJson(f.verified_evidence?.rects)[0];
                                if (r) setPage(r.page_no);
                                else if (f.verified_evidence?.page != null) setPage(f.verified_evidence.page);
                                setMobilePane("page");
                              }}
                            >
                              <span className="review__ev-kind review__ev-kind--human">clause</span>
                              <span className="review__ev-snip">{f.verified_evidence.snippet}</span>
                              {f.verified_evidence.page != null && <span className="review__ev-pg">p{f.verified_evidence.page}</span>}
                            </div>
                          )}
                          <div className="review__corr-note">
                            The AI's original extraction stays below for the audit trail.
                          </div>
                        </div>
                      )}
                      {isCorrected && activeKey === key && f.values.length > 0 && (
                        <div className="review__ai-label">AI's original extraction (superseded)</div>
                      )}
                      <div className={`review__field-val${activeKey === key ? " is-open" : ""}${isCorrected && activeKey === key ? " is-demoted" : ""}`}>
                        {f.values.length === 0
                          ? <em className="review__ns">— not stated</em>
                          : (activeKey === key ? f.values : f.values.slice(0, 2)).map((v, i) => (
                            <span
                              key={i}
                              className={`review__val${activeKey === key && i === focusIdx && f.values.length > 1 ? " is-focus" : ""}`}
                              title={String(v.value)}
                              onClick={activeKey === key
                                ? (e) => { e.stopPropagation(); focusValue(i); }
                                : undefined}
                            >
                              {String(v.value)}{v.n_mentions > 1 && <sup>{v.n_mentions}×</sup>}
                            </span>
                          ))}
                        {activeKey !== key && f.values.length > 2 && (
                          <span className="review__more">+{f.values.length - 2} more</span>
                        )}
                      </div>
                      {activeKey === key && (
                        <div className="review__evidence" onClick={e => e.stopPropagation()}>
                          {f.values.length > 1 && (
                            <div className="review__valnav">
                              <button onClick={() => focusValue((focusIdx + f.values.length - 1) % f.values.length)}>◄</button>
                              <span>value {focusIdx + 1} of {f.values.length}</span>
                              <button onClick={() => focusValue((focusIdx + 1) % f.values.length)}>►</button>
                              <em>Click a value or a page highlight to focus it.</em>
                            </div>
                          )}
                          {f.values.flatMap(v => v.evidence).length === 0 && (
                            <div className="review__no-ev">
                              {f.method === "external"
                                ? "This value comes from outside the document by design, so there is no clause to highlight."
                                : "No page evidence attached to this value."}
                            </div>
                          )}
                          {f.values.length > 1 && (fv?.evidence.length ?? 0) > 0 && (
                            <div className="review__ai-label">
                              Evidence for: {String(fv?.value ?? "").slice(0, 60)}
                            </div>
                          )}
                          {(f.values.length > 1
                            ? (fv?.evidence ?? []).map(ev => ({ ev, val: String(fv?.value ?? "") }))
                            : f.values.flatMap(v => v.evidence.map(ev => ({ ev, val: String(v.value) }))))
                            .slice(0, 8).map(({ ev, val }, i) => (
                            <div
                              key={i}
                              className="review__ev"
                              onClick={() => {
                                const pages = [...new Set(parseRects(ev).map(r => r.page_no))];
                                const best = distinctivePage(f, pages) ?? ev.page;
                                if (best != null) setPage(best);
                                setMobilePane("page");
                              }}
                            >
                              <span className={`review__ev-kind review__ev-kind--${ev.kind}`}>{ev.kind}</span>
                              <span className="review__ev-snip">{ev.snippet}</span>
                              {ev.page != null && <span className="review__ev-pg">p{ev.page}</span>}
                              {f.can_edit && (
                                <button
                                  className="review__ev-x"
                                  title="Wrong clause? Remove this citation from the field."
                                  disabled={busy === key}
                                  onClick={e => { e.stopPropagation(); doRejectEvidence(key, ev, val); }}
                                >✕</button>
                              )}
                            </div>
                          ))}
                          {(f.votes?.length ?? 0) > 0 && (
                            <div className="review__tally">
                              {f.votes!.map((v, i) => (
                                <span
                                  key={i}
                                  className={`review__vote review__vote--${v.decision}${v.voter === myEmail ? " is-me" : ""}`}
                                  title={`${v.voter_name ?? v.voter} · ${v.decision === "reject" ? "rejected" : v.value ? `proposed "${v.value}"` : "approved"}${v.comment ? ` · ${v.comment}` : ""}`}
                                >
                                  {v.decision === "reject" ? "✗" : v.value ? "✎" : "✓"} {voterLabel(v)}
                                </span>
                              ))}
                            </div>
                          )}
                          {f.can_edit && pendingAmendments(f)
                            .filter(a => !(f.verified && f.verified_value === a.value))
                            .map(a => {
                              const need = Math.max(0, cq - a.count);
                              return (
                                <div key={a.value} className="review__amend">
                                  <span className="review__amend-val" title={a.value}>✎ "{a.value}"</span>
                                  <span className="review__amend-n">
                                    {a.count} vote{a.count > 1 ? "s" : ""}
                                    {need > 0 ? ` · needs ${need} more to concur` : ""}
                                  </span>
                                  {!(f.my_vote?.decision === "approve" && f.my_vote?.value === a.value) && (
                                    <button
                                      disabled={busy === key}
                                      title="Agree with this proposed correction. Your vote moves to it."
                                      onClick={() => doVote(key, "approve", a.value)}
                                    >Concur</button>
                                  )}
                                </div>
                              );
                            })}
                          {f.can_edit && editingKey !== key && (
                            <div className="review__actions">
                              <button
                                disabled={busy === key || (f.my_vote?.decision === "approve" && !f.my_vote?.value)}
                                title="The extracted value is correct"
                                onClick={() => doVote(key, "approve")}
                              >✓ Approve</button>
                              <button
                                disabled={busy === key || f.my_vote?.decision === "reject"}
                                title="The extracted value is wrong (no correction proposed)"
                                onClick={() => doVote(key, "reject")}
                              >✗ Reject</button>
                              <button
                                disabled={busy === key}
                                title="Propose the corrected value. It takes effect once another verifier concurs."
                                onClick={() => {
                                  setEditingKey(key);
                                  setEditValue(String(f.my_vote?.value ?? f.verified_value ?? f.values[0]?.value ?? ""));
                                }}
                              >✎ Propose correction</button>
                            </div>
                          )}
                          {f.can_edit && editingKey !== key && f.my_vote && (
                            <div className="review__myvote">
                              Your vote: {f.my_vote.decision === "reject" ? "✗ reject" : f.my_vote.value ? `✎ "${f.my_vote.value}"` : "✓ approve"}. Pick another action to change it.
                            </div>
                          )}
                          {f.can_edit && editingKey === key && (
                            <div className="review__editor">
                              <label>Corrected value (the AI's clause stays in the record for audit)</label>
                              <textarea
                                value={editValue}
                                onChange={e => setEditValue(e.target.value)}
                                rows={Math.min(6, Math.max(2, Math.ceil(editValue.length / 60)))}
                                autoFocus
                              />
                              <div className="review__editor-clause">
                                <button
                                  className={`review__editor-mark${corrPick ? " is-on" : ""}`}
                                  onClick={() => { setCorrPick(p => !p); setAddMode(false); setSelected([]); }}
                                >
                                  {corrPick ? "✕ Stop marking" : "▦ Mark the clause on the page"}
                                </button>
                                <span>
                                  {corrPick
                                    ? (selected.length
                                        ? `${selected.length} block${selected.length > 1 ? "s" : ""} selected`
                                        : "Click the clause(s) on the page that state the corrected value.")
                                    : "Recommended: anchor the correction to its clause so the highlight stays truthful."}
                                </span>
                              </div>
                              <div className="review__editor-actions">
                                <button
                                  className="review__editor-cancel"
                                  onClick={() => { setEditingKey(null); setCorrPick(false); setSelected([]); }}
                                >Cancel</button>
                                <button
                                  className="review__editor-save"
                                  disabled={busy === key || !editValue.trim()}
                                  onClick={async () => {
                                    const ev = corrPick || selected.length ? selectedEvidence() : undefined;
                                    await doVote(key, "approve", editValue.trim(), ev);
                                    setEditingKey(null); setCorrPick(false); setSelected([]);
                                  }}
                                >{cq > 1 ? "Propose (needs 1 concur)" : "Propose as correction"}</button>
                              </div>
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                  );
                })}
              </div>
              );
            })}
            </>
          )}
        </div>
      </div>
      )}
    </div>
  );
}
