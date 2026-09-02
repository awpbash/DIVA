import { useEffect, useMemo, useRef, useState } from "react";
import { cleanTitle, familyOf, groupDocs, shortDate, shortType } from "../docmeta";
import { DocumentMeta } from "../types";
import { IconCheck, IconChevron, IconDocument, IconEye } from "./Icon";

/**
 * Multi-select document scope — a compact dropdown (not an always-open list,
 * so it stays one line however many contracts are loaded).
 *
 * Drill-in navigation, same mental model as the admin and review lists: the
 * top level shows one row per folder (contract family), clicking one enters
 * it, a breadcrumb goes back. Searching at the top level also surfaces
 * matching documents, and picking one jumps into its folder.
 *
 * The backend takes `doc_ids` as a LIST and every retrieval tool filters
 * `doc_id IN $doc_ids`, so selecting a subset searches exactly those. Empty
 * selection = all documents (cross-doc). Opening a contract in the PDF viewer
 * (the eye button) is separate and never changes the search scope.
 *
 * The panel is `position: fixed` (anchored to the trigger) so it escapes the
 * sidebar's `overflow: hidden` clip.
 */
interface Props {
  docs: DocumentMeta[];
  /** Contracts in the search scope. Empty = all (cross-doc). */
  selectedDocIds: string[];
  /** The contract currently shown in the PDF viewer. */
  viewerDocId: string | null;
  onToggleScope: (id: string) => void;
  onClearScope: () => void;
  onOpenDoc: (id: string) => void;
}

export function DocCatalog({
  docs, selectedDocIds, viewerDocId, onToggleScope, onClearScope, onOpenDoc,
}: Props) {
  const [open, setOpen] = useState(false);
  const [rect, setRect] = useState<DOMRect | null>(null);
  const [query, setQuery] = useState("");
  // The folder currently drilled into. null = the folder list.
  const [openFam, setOpenFam] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  const total = docs.length;
  const k = selectedDocIds.length;
  const allMode = k === 0;

  // Grouped by contract family, base contract first, amendments in date order —
  // a folder reads as "this agreement and everything that changed it".
  const grouped = useMemo(() => groupDocs(docs), [docs]);
  const q = query.trim().toLowerCase();
  const titleHit = (d: DocumentMeta) => cleanTitle(d.title).toLowerCase().includes(q);

  // Top level: folder rows filtered by name, plus doc-title matches that jump
  // into their folder (the query is kept, so the match stays in view inside).
  const famRows = q ? grouped.filter(g => g.family.toLowerCase().includes(q)) : grouped;
  const docHits = q ? docs.filter(titleHit) : [];
  // Inside a folder: its docs, filtered by the same query.
  const famGroup = openFam ? grouped.find(g => g.family === openFam) ?? null : null;
  const famDocs = famGroup ? (q ? famGroup.docs.filter(titleHit) : famGroup.docs) : [];

  const toggleOpen = () => {
    if (!open && triggerRef.current) {
      setRect(triggerRef.current.getBoundingClientRect());
      setOpenFam(null);
    }
    setOpen(o => !o);
  };

  // Close on outside click / Esc / scroll / resize — standard dropdown.
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (triggerRef.current?.contains(t) || popRef.current?.contains(t)) return;
      setOpen(false);
    };
    // Capture-phase scroll closes the panel when the PAGE scrolls (the fixed
    // panel would otherwise detach from its trigger) — but NOT when the user
    // scrolls the panel's own list, which previously slammed it shut on the
    // first wheel tick.
    const onScroll = (e: Event) => {
      const t = e.target as Node | null;
      if (t && popRef.current?.contains(t)) return;
      setOpen(false);
    };
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [open]);

  const summary = total === 0
    ? "No documents"
    : allMode ? `All ${total} documents` : `${k} of ${total} selected`;

  const panelStyle: React.CSSProperties = rect
    ? {
        position: "fixed",
        top: rect.bottom + 4,
        left: rect.left,
        width: Math.max(rect.width, 248),
        maxHeight: Math.min(380, window.innerHeight - rect.bottom - 16),
      }
    : { display: "none" };

  return (
    <div className="catalog">
      <div className="catalog__label">Documents</div>
      <button
        ref={triggerRef}
        type="button"
        className={`catalog__trigger${open ? " catalog__trigger--open" : ""}`}
        onClick={toggleOpen}
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={total === 0}
      >
        <IconDocument size={14} />
        <span className="catalog__trigger-label">{summary}</span>
        <IconChevron size={12} />
      </button>

      {open && (
        <div ref={popRef} className="catalog__pop" style={panelStyle} role="listbox">
          <div className="catalog__pop-head">
            <span>Search scope</span>
            <span className="catalog__stat">{total} loaded</span>
          </div>

          {total > 6 && (
            <input
              className="catalog__search"
              placeholder="Filter by name or family…"
              value={query}
              onChange={e => setQuery(e.target.value)}
              autoFocus
            />
          )}

          <button
            type="button"
            className={`catalog__all${allMode ? " catalog__all--on" : ""}`}
            onClick={onClearScope}
          >
            <span className={`doc-card__check${allMode ? " doc-card__check--on" : ""}`}>
              {allMode && <IconCheck size={11} />}
            </span>
            <span>All documents</span>
          </button>

          <div className="catalog__list">
            {openFam === null ? (
              <>
                {famRows.map(g => (
                  <button
                    key={g.family}
                    type="button"
                    className="catalog__fold"
                    onClick={() => setOpenFam(g.family)}
                    title="Open this folder"
                  >
                    <span className="catalog__fold-name">{g.family}</span>
                    <span className="catalog__fold-n">{g.docs.length}</span>
                  </button>
                ))}
                {docHits.length > 0 && (
                  <>
                    <div className="catalog__fam">
                      <span>Matching documents</span>
                      <span className="catalog__fam-n">{docHits.length}</span>
                    </div>
                    {docHits.map(d => (
                      <button
                        key={d.doc_id}
                        type="button"
                        className="catalog__fold"
                        onClick={() => setOpenFam(familyOf(d))}
                        title="Open this document's folder"
                      >
                        <span className="catalog__fold-name">
                          {cleanTitle(d.title)}
                          <span className="catalog__fold-sub">in {familyOf(d)}</span>
                        </span>
                      </button>
                    ))}
                  </>
                )}
                {famRows.length === 0 && docHits.length === 0 && (
                  <div className="catalog__empty">No documents match “{query}”.</div>
                )}
              </>
            ) : (
              <>
                <div className="catalog__crumbs">
                  <button type="button" className="catalog__crumb" onClick={() => setOpenFam(null)}>
                    Documents
                  </button>
                  <span className="catalog__crumb-sep">›</span>
                  <span className="catalog__crumb-cur">{openFam}</span>
                </div>
                {famDocs.map(d => {
                  const inScope = selectedDocIds.includes(d.doc_id);
                  const viewing = d.doc_id === viewerDocId;
                  const meta = [shortType(d.doc_type), shortDate(d.doc_date_iso),
                                `${d.total_pages}pp`].filter(Boolean).join(" · ");
                  return (
                    <div
                      key={d.doc_id}
                      className={[
                        "doc-card",
                        inScope && "doc-card--on",
                        viewing && "doc-card--viewing",
                      ].filter(Boolean).join(" ")}
                      onClick={() => onToggleScope(d.doc_id)}
                      role="option"
                      aria-selected={inScope}
                      tabIndex={0}
                      onKeyDown={e => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          onToggleScope(d.doc_id);
                        }
                      }}
                      title={inScope
                        ? "In search scope. Click to remove."
                        : "Click to search only the selected documents"}
                    >
                      <span className={`doc-card__check${inScope ? " doc-card__check--on" : ""}`}>
                        {inScope && <IconCheck size={11} />}
                      </span>
                      <div className="doc-card__body">
                        <div className="doc-card__title">{cleanTitle(d.title)}</div>
                        <div className="doc-card__meta">
                          {meta}
                          {d.review_tier && d.review_tier !== "unreviewed" && (
                            <span
                              className={`doc-card__trust doc-card__trust--${d.review_tier}`}
                              title={`${d.review_verified}/${d.review_populated} extracted fields human-verified`}
                            >
                              ✓ {d.review_tier === "reviewed" ? "reviewed" : "partly reviewed"}
                            </span>
                          )}
                        </div>
                      </div>
                      <button
                        type="button"
                        className={`doc-card__view${viewing ? " doc-card__view--on" : ""}`}
                        onClick={e => { e.stopPropagation(); onOpenDoc(d.doc_id); }}
                        title="Open in PDF viewer"
                        aria-label="Open in PDF viewer"
                      >
                        <IconEye size={13} />
                      </button>
                    </div>
                  );
                })}
                {famDocs.length === 0 && (
                  <div className="catalog__empty">
                    {q ? `No documents here match “${query}”.` : "This folder is empty."}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
