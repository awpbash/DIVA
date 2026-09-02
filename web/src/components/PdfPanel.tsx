import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import { apiUrl, authHeaders, getRestrictedRegions } from "../api";
import { cleanTitle } from "../docmeta";
import { Citation, CitationRect, DocumentMeta } from "../types";
import {
  IconArrowLeft, IconArrowRight, IconDocument, IconClose, IconLayers,
} from "./Icon";
import { PageJump } from "./PageJump";

// Worker URL — resolved against the installed pdfjs-dist version (pinned in
// package.json to match what react-pdf bundles). Vite ?url turns it into a
// hashed asset path served by the dev server / built into dist/.
import pdfjsWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";
pdfjs.GlobalWorkerOptions.workerSrc = pdfjsWorker;

import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";

/** Per-page rects for a citation; falls back to the envelope bbox for
 * spans loaded before rects existed. */
function rectsFor(c: Citation): CitationRect[] {
  if (c.rects && c.rects.length) return c.rects;
  return c.bbox ? [{ page_no: c.page_no, bbox: c.bbox }] : [];
}

// Rects covering an absurd fraction of the page are skipped — figure blocks
// or snippet-fallback page-spanners. Generous on height (a multi-line table
// row / long clause is a legitimate tall highlight); the area cap is the real
// page-spanner guard.
const MAX_BBOX_HEIGHT = 0.55;
const MAX_BBOX_AREA = 0.40;
const saneRect = (b: [number, number, number, number]) => {
  const h = b[3] - b[1];
  const w = b[2] - b[0];
  return h <= MAX_BBOX_HEIGHT && w * h <= MAX_BBOX_AREA;
};

interface Props {
  doc: DocumentMeta | null;
  focused: Citation | null;
  /** The answer's cited evidence — the set the stepper walks. The PDF shows
   * one document, so only cluster members in `doc` are navigable here. */
  cluster?: Citation[];
  /** Step focus to another citation (so the chat chip + page follow). */
  onFocusCitation?: (c: Citation) => void;
  /** Hide the PDF pane. */
  onClose?: () => void;
  /** Global "Restricted view" — blur highlight regions of sensitive evidence. */
  restricted?: boolean;
  /** Bumped when the access policy changes, to force a blur-region refetch. */
  policyVersion?: number;
}

interface SearchHit { page: number; bbox: [number, number, number, number]; }

export function PdfPanel({ doc, focused, cluster = [], onFocusCitation, onClose, restricted, policyVersion }: Props) {
  const [numPages, setNumPages] = useState(0);
  const [currentPage, setCurrentPage] = useState(1);
  const [pageWidth, setPageWidth] = useState(720);
  const [showAllOnPage, setShowAllOnPage] = useState(false);
  // Find-in-document over the PDF's text layer (digital PDFs; a pure scan has
  // no text layer, in which case the search honestly reports zero matches).
  const pdfRef = useRef<{ numPages: number; getPage: (n: number) => Promise<unknown> } | null>(null);
  const [searchQ, setSearchQ] = useState("");
  const [searchHits, setSearchHits] = useState<SearchHit[] | null>(null);
  const [searchIdx, setSearchIdx] = useState(0);
  const [searching, setSearching] = useState(false);
  // Whole-document blur regions for restricted view — every sensitive
  // (financial) region in this doc, so a value can't be read off the page even
  // when no answer cited it. Fetched per doc; cleared in full view.
  const [redactRects, setRedactRects] = useState<CitationRect[]>([]);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!restricted || !doc) { setRedactRects([]); return; }
    let live = true;
    getRestrictedRegions(doc.doc_id)
      .then(rs => { if (live) setRedactRects(rs); })
      .catch(() => { if (live) setRedactRects([]); });
    return () => { live = false; };
  }, [restricted, doc?.doc_id, policyVersion]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(entries => {
      for (const e of entries) {
        const w = Math.min(e.contentRect.width - 36, 980);
        if (w > 200) setPageWidth(w);
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // The stepper set: cluster members in THIS document, ordered by where they
  // sit on the page (page, then top-to-bottom), deduped. `focused` is always
  // included even when no cluster was supplied (e.g. an Explore-graph jump).
  const inDoc = useMemo(() => {
    const base = cluster.length ? [...cluster] : [];
    if (focused && !base.some(c => c.evidence_id === focused.evidence_id)) {
      base.push(focused);
    }
    const byId = new Map<string, Citation>();
    for (const c of base) {
      if (c.doc_id === doc?.doc_id && c.evidence_id) byId.set(c.evidence_id, c);
    }
    const pos = (c: Citation): [number, number] => {
      const r = rectsFor(c)[0];
      return [r?.page_no ?? c.page_no ?? 0, r?.bbox?.[1] ?? 0];
    };
    return [...byId.values()].sort((a, b) => {
      const [pa, ya] = pos(a);
      const [pb, yb] = pos(b);
      return pa - pb || ya - yb;
    });
  }, [cluster, focused, doc?.doc_id]);

  const activeIdx = focused
    ? inDoc.findIndex(c => c.evidence_id === focused.evidence_id)
    : -1;

  const step = useCallback((delta: number) => {
    if (activeIdx < 0 || !onFocusCitation) return;
    const next = inDoc[activeIdx + delta];
    if (next) onFocusCitation(next);
  }, [activeIdx, inDoc, onFocusCitation]);

  // Choose the page to show whenever the focused citation OR the document
  // changes. If the focused citation belongs to THIS document, jump to its
  // page; otherwise (a doc-link opened a different doc while a citation in
  // another doc was focused, or there's no focus) start at page 1 — never
  // carry a stale page number across a document switch ("Page 13 of 2").
  useEffect(() => {
    if (focused && (!doc || focused.doc_id === doc.doc_id)) {
      const first = rectsFor(focused)[0];
      setCurrentPage(first?.page_no || focused.page_no || 1);
    } else {
      setCurrentPage(1);
    }
  }, [focused, doc?.doc_id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-scroll the viewer so the active highlight is in view — flipping the
  // page isn't enough when the highlight sits below the fold.
  useEffect(() => {
    if (!focused) return;
    const viewer = containerRef.current;
    if (!viewer) return;
    const t = window.setTimeout(() => {
      const hl = viewer.querySelector(".pdf__highlight--active") as HTMLElement | null;
      if (!hl) return;
      const vRect = viewer.getBoundingClientRect();
      const hRect = hl.getBoundingClientRect();
      const delta = hRect.top - vRect.top - vRect.height * 0.32;
      viewer.scrollTo({ top: viewer.scrollTop + delta, behavior: "smooth" });
    }, 160);
    return () => window.clearTimeout(t);
  }, [focused?.evidence_id, currentPage, pageWidth]); // eslint-disable-line react-hooks/exhaustive-deps

  const file = useMemo(() => {
    if (!doc) return null;
    const url = apiUrl(`/pdf/${doc.doc_id}`);
    const headers = authHeaders();
    return Object.keys(headers).length ? { url, httpHeaders: headers } : url;
  }, [doc]);

  const onLoad = useCallback((pdf: { numPages: number; getPage: (n: number) => Promise<unknown> }) => {
    pdfRef.current = pdf;
    setNumPages(pdf.numPages);
    // Defensive clamp: if a previous (longer) doc left us on a page this doc
    // doesn't have, snap back to page 1 instead of rendering a blank page.
    setCurrentPage(p => (p > pdf.numPages ? 1 : p));
  }, []);

  // A document switch invalidates every search result.
  useEffect(() => {
    setSearchQ(""); setSearchHits(null); setSearchIdx(0);
  }, [doc?.doc_id]);

  const runSearch = useCallback(async () => {
    const pdf = pdfRef.current;
    const q = searchQ.trim().toLowerCase();
    if (!pdf || q.length < 2) { setSearchHits(null); return; }
    setSearching(true);
    const hits: SearchHit[] = [];
    try {
      for (let p = 1; p <= pdf.numPages && hits.length < 200; p++) {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        const pg: any = await pdf.getPage(p);
        const vp = pg.getViewport({ scale: 1 });
        const tc = await pg.getTextContent();
        for (const item of tc.items as any[]) {
          if (!item.str || !String(item.str).toLowerCase().includes(q)) continue;
          // Map the text item (baseline origin, PDF space) into normalised
          // top-left page coordinates — the same space citation bboxes use.
          const m = (pdfjs as any).Util.transform(vp.transform, item.transform);
          const h = item.height || Math.hypot(m[2], m[3]) || 10;
          const w = item.width || 10;
          hits.push({
            page: p,
            bbox: [
              Math.max(0, m[4] / vp.width),
              Math.max(0, (m[5] - h) / vp.height),
              Math.min(1, (m[4] + w) / vp.width),
              Math.min(1, m[5] / vp.height),
            ],
          });
          if (hits.length >= 200) break;
        }
        /* eslint-enable @typescript-eslint/no-explicit-any */
      }
    } finally {
      setSearching(false);
    }
    setSearchHits(hits);
    setSearchIdx(0);
    if (hits.length) setCurrentPage(hits[0].page);
  }, [searchQ]);

  const gotoHit = useCallback((delta: number) => {
    setSearchHits(hits => {
      if (!hits || hits.length === 0) return hits;
      setSearchIdx(i => {
        const next = (i + delta + hits.length) % hits.length;
        setCurrentPage(hits[next].page);
        return next;
      });
      return hits;
    });
  }, []);

  // Highlights for the current page: the focused citation always; the rest of
  // the cluster too when "all on page" is on (dimmed, focused emphasised).
  const highlightsOnPage = useMemo(() => {
    if (!focused) return [];
    const activeId = focused.evidence_id;
    const sources = showAllOnPage ? inDoc : inDoc.filter(c => c.evidence_id === activeId);
    const out: { key: string; bbox: [number, number, number, number];
                 active: boolean; blur: boolean }[] = [];
    for (const c of sources) {
      const blur = !!(restricted && c.sensitivity);
      rectsFor(c)
        .filter(r => r.page_no === currentPage && saneRect(r.bbox))
        .forEach((r, i) => out.push({
          key: `${c.evidence_id}:${i}`,
          bbox: r.bbox,
          active: c.evidence_id === activeId,
          blur,
        }));
    }
    return out;
  }, [focused, inDoc, showAllOnPage, currentPage, restricted]);

  // Always-on blur for restricted regions on the current page (independent of
  // any focused citation — the gate must hold while simply browsing the PDF).
  const redactOnPage = useMemo(
    () => redactRects
      .filter(r => r.page_no === currentPage && saneRect(r.bbox))
      .map(r => r.bbox),
    [redactRects, currentPage],
  );

  const header = (
    <div className="pane__header">
      <IconDocument />
      <div className="pane__title" title={doc?.title}>{doc ? cleanTitle(doc.title) : "Source"}</div>
      {doc && <div className="pane__subtitle">· source PDF</div>}
      <div className="pane__header-spacer" />
      {onClose && (
        <button
          type="button"
          className="pane__icon-btn"
          onClick={onClose}
          aria-label="Hide source"
          title="Hide source"
        >
          <IconClose />
        </button>
      )}
    </div>
  );

  if (!doc) {
    return (
      <div className="pane pane--source pdf">
        {header}
        <div className="pdf__placeholder">Pick a document from the sidebar to view it.</div>
      </div>
    );
  }

  return (
    <div className="pane pane--source pdf">
      {header}

      <div className="pdf__searchbar">
        <input
          className="pdf__search-input"
          placeholder="Find in document…"
          value={searchQ}
          onChange={e => setSearchQ(e.target.value)}
          onKeyDown={e => {
            if (e.key === "Enter") { e.preventDefault(); searchHits ? gotoHit(1) : runSearch(); }
            if (e.key === "Escape") { setSearchQ(""); setSearchHits(null); }
          }}
        />
        {searching ? (
          <span className="pdf__search-count">searching…</span>
        ) : searchHits ? (
          <>
            <span className="pdf__search-count">
              {searchHits.length === 0 ? "no matches" : `${searchIdx + 1} / ${searchHits.length}`}
            </span>
            <button type="button" className="pdf__ev-nav" onClick={() => gotoHit(-1)}
              disabled={searchHits.length === 0} aria-label="Previous match">
              <IconArrowLeft size={12} />
            </button>
            <button type="button" className="pdf__ev-nav" onClick={() => gotoHit(1)}
              disabled={searchHits.length === 0} aria-label="Next match">
              <IconArrowRight size={12} />
            </button>
            <button type="button" className="pdf__search-clear" aria-label="Clear search"
              onClick={() => { setSearchQ(""); setSearchHits(null); }}>×</button>
          </>
        ) : searchQ.trim().length >= 2 ? (
          <button type="button" className="pdf__search-go" onClick={runSearch}>Find</button>
        ) : null}
      </div>

      {focused && inDoc.length > 0 && (
        <div className="pdf__evidence-bar">
          <button
            type="button"
            className="pdf__ev-nav"
            onClick={() => step(-1)}
            disabled={activeIdx <= 0}
            aria-label="Previous evidence"
            title="Previous evidence"
          >
            <IconArrowLeft size={13} />
          </button>
          <span className="pdf__ev-count">
            Evidence {activeIdx >= 0 ? activeIdx + 1 : 1} / {inDoc.length}
          </span>
          <button
            type="button"
            className="pdf__ev-nav"
            onClick={() => step(1)}
            disabled={activeIdx < 0 || activeIdx >= inDoc.length - 1}
            aria-label="Next evidence"
            title="Next evidence"
          >
            <IconArrowRight size={13} />
          </button>

          <div className="pdf__ev-meta">
            {focused.section_num && <span className="pdf__ev-sec">§{focused.section_num}</span>}
            <span className="pdf__ev-page">p{focused.page_no}</span>
            {focused.fact_label && <span className="pdf__ev-tag">{focused.fact_label}</span>}
            {focused.confidence_tier && (
              <span className={`pdf__ev-tier pdf__ev-tier--${focused.confidence_tier}`}>
                {focused.confidence_tier}
              </span>
            )}
            {/* Verification consensus, first-class (was tooltip-only): who
                stands behind this value and the vote share. */}
            {focused.field_trust === "human_validated" && focused.verified_by?.length ? (
              <span
                className="pdf__ev-verify pdf__ev-verify--ok"
                title={`Verified by ${focused.verified_by.join(", ")}`}
              >
                ✓ Human-verified
                {focused.verify_confidence != null && focused.verify_votes
                  ? ` ${Math.round(focused.verify_confidence * 100)}% (${focused.verified_by.length}/${focused.verify_votes})`
                  : ""}
              </span>
            ) : focused.field_trust === "disputed" ? (
              <span className="pdf__ev-verify pdf__ev-verify--disputed" title="Verifiers disagree on this value">
                ⚠ Disputed
              </span>
            ) : focused.fact_label === "OpsField" ? (
              <span className="pdf__ev-verify pdf__ev-verify--none">AI-extracted, unreviewed</span>
            ) : null}
          </div>

          <div className="pdf__ev-spacer" />
          {inDoc.length > 1 && (
            <button
              type="button"
              className={`pdf__ev-all${showAllOnPage ? " pdf__ev-all--on" : ""}`}
              onClick={() => setShowAllOnPage(v => !v)}
              title="Highlight every cited paragraph on this page"
            >
              <IconLayers size={12} />
              <span>all on page</span>
            </button>
          )}
        </div>
      )}

      <div className="pdf__viewer" ref={containerRef}>
        {file && (
          <Document
            file={file}
            onLoadSuccess={onLoad}
            loading={<div className="pdf__placeholder">Loading PDF…</div>}
            error={<div className="pdf__placeholder">Couldn't load this PDF.</div>}
          >
            <div className="pdf__page-wrap">
              <Page
                pageNumber={currentPage}
                width={pageWidth}
                renderAnnotationLayer={false}
                renderTextLayer={false}
              />
              {redactOnPage.map((b, i) => (
                <HighlightBox
                  key={`redact-${i}`}
                  bbox={b}
                  active={false}
                  blur
                  width={pageWidth}
                />
              ))}
              {highlightsOnPage.map(h => (
                <HighlightBox
                  key={h.key}
                  bbox={h.bbox}
                  active={h.active}
                  blur={h.blur}
                  width={pageWidth}
                />
              ))}
              {(searchHits ?? [])
                .map((h, i) => ({ ...h, i }))
                .filter(h => h.page === currentPage)
                .map(h => (
                  <HighlightBox
                    key={`search-${h.i}`}
                    bbox={h.bbox}
                    active={h.i === searchIdx}
                    width={pageWidth}
                    search
                  />
                ))}
            </div>
          </Document>
        )}
      </div>

      <div className="pdf__controls">
        <button
          onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
          disabled={currentPage <= 1}
          aria-label="Previous page"
        >
          <IconArrowLeft size={14} />
        </button>
        <span className="pdf__pageno">
          Page <PageJump page={currentPage} total={numPages} onJump={setCurrentPage} /> of {numPages || "?"}
        </span>
        <button
          onClick={() => setCurrentPage(p => Math.min(numPages, p + 1))}
          disabled={currentPage >= numPages}
          aria-label="Next page"
        >
          <IconArrowRight size={14} />
        </button>
        <div className="pdf__controls__spacer" />
        <span>
          {highlightsOnPage.length > 0
            ? showAllOnPage && inDoc.length > 1
              ? `${highlightsOnPage.length} highlight${highlightsOnPage.length === 1 ? "" : "s"} on this page`
              : "Highlighted on this page"
            : focused && focused.doc_id === doc.doc_id
              ? `Highlight is on page ${rectsFor(focused)[0]?.page_no || focused.page_no}`
              : "Click a citation to highlight a paragraph"}
        </span>
      </div>
    </div>
  );
}

interface HighlightBoxProps {
  bbox: [number, number, number, number];
  active: boolean;
  width: number;
  /** Restricted (financial) region — render as a blur/redaction cover. */
  blur?: boolean;
  /** Find-in-document match — outlined instead of citation-filled. */
  search?: boolean;
}

function HighlightBox({ bbox, active, width, blur, search }: HighlightBoxProps) {
  const [pageHeight, setPageHeight] = useState<number>(0);
  // Canvas may not be flush with .pdf__page-wrap's top (react-pdf can wrap
  // it with its own div). Anchor the highlight to the canvas, not the wrap.
  const [canvasOffsetY, setCanvasOffsetY] = useState<number>(0);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const parent = ref.current?.parentElement;
    if (!parent) return;
    const measure = () => {
      const canvas = parent.querySelector("canvas") as HTMLCanvasElement | null;
      if (!canvas) return;
      const cRect = canvas.getBoundingClientRect();
      const pRect = parent.getBoundingClientRect();
      if (cRect.height > 0) {
        setPageHeight(cRect.height);
        setCanvasOffsetY(cRect.top - pRect.top);
      }
    };
    measure();
    const ro = new ResizeObserver(measure);
    if (parent) ro.observe(parent);
    return () => ro.disconnect();
  }, [width]);

  if (!pageHeight) return <div ref={ref} style={{ display: "none" }} />;

  const [x0, y0, x1, y1] = bbox;
  // Bboxes are produced by RapidOCR's pixel-precise line polygons (normalised
  // against the same pixmap that renders into pdfjs), so no offset is needed.
  const top = canvasOffsetY + y0 * pageHeight;
  const style: React.CSSProperties = {
    left: `${x0 * width}px`,
    top: `${top}px`,
    width: `${(x1 - x0) * width}px`,
    height: `${(y1 - y0) * pageHeight}px`,
  };
  return (
    <div
      ref={ref}
      className={
        search
          ? `pdf__highlight pdf__highlight--search${active ? " pdf__highlight--search-active" : ""}`
          : `pdf__highlight ${active ? "pdf__highlight--active" : "pdf__highlight--dim"}`
            + (blur ? " pdf__highlight--blur" : "")
      }
      style={style}
      title={blur ? "Restricted. Hidden for your access level." : undefined}
    />
  );
}
