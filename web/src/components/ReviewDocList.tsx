import { useEffect, useMemo, useState } from "react";
import { HeatmapDoc, getReviewHeatmap, listDocuments } from "../api";
import { cleanTitle, familyOf, shortDate, shortType, sortDocs } from "../docmeta";
import { DocumentMeta } from "../types";
import { loadUi, saveUi } from "../uiState";
import "./ReviewDocList.css";

// The review overview as a DRILL-IN folder list (SharePoint-library style): the
// top level shows one row per folder (contract family) with its verification
// progress rolled up over the member documents, plus a "Loose documents" row.
// Clicking a folder enters it and lists its documents, a breadcrumb goes back.
// This is the default view. The field-by-category heatmap stays available
// behind a toggle for admins who want the per-category detail, but the first
// question is always "which documents can I trust?", answered per document.

interface Props {
  onPick: (docId: string) => void;
}

interface Row {
  doc: DocumentMeta;
  populated: number;
  verified: number;
}

interface Fam {
  family: string;
  rows: Row[];
  populated: number;
  verified: number;
}

type Status = "reviewed" | "in_review" | "unreviewed" | "empty";

function statusOf(r: { populated: number; verified: number }): Status {
  if (r.populated === 0) return "empty";
  if (r.verified === 0) return "unreviewed";
  if (r.verified >= r.populated) return "reviewed";
  return "in_review";
}

const STATUS_TEXT: Record<Status, string> = {
  reviewed: "Reviewed",
  in_review: "In review",
  unreviewed: "Not reviewed yet",
  empty: "Nothing extracted",
};

export function ReviewDocList({ onPick }: Props) {
  const [docs, setDocs] = useState<DocumentMeta[]>([]);
  const [hm, setHm] = useState<Record<string, HeatmapDoc>>({});
  const [err, setErr] = useState<string | null>(null);
  // The folder currently drilled into. null = the folder list. Survives tab
  // switches so coming back lands in the same folder. A stale name falls back
  // to the folder list naturally (the lookup below misses).
  const [openFam, setOpenFamState] = useState<string | null>(() => loadUi<string | null>("review.fam", null));
  const setOpenFam = (fam: string | null) => { setOpenFamState(fam); saveUi("review.fam", fam); };

  useEffect(() => {
    Promise.all([listDocuments(), getReviewHeatmap()])
      .then(([d, h]) => {
        setDocs(sortDocs(d));
        setHm(Object.fromEntries(h.docs.map(x => [x.doc_id, x])));
      })
      .catch(() => setErr("Couldn't load the document overview."));
  }, []);

  const rows: Row[] = useMemo(
    () => docs.map(doc => ({
      doc,
      populated: hm[doc.doc_id]?.n_populated ?? 0,
      verified: hm[doc.doc_id]?.n_verified ?? 0,
    })),
    [docs, hm],
  );

  // Folder roll-up. rows arrive family-sorted, so adjacent grouping is enough.
  const fams: Fam[] = useMemo(() => {
    const out: Fam[] = [];
    for (const r of rows) {
      const fam = familyOf(r.doc);
      if (!out.length || out[out.length - 1].family !== fam) {
        out.push({ family: fam, rows: [], populated: 0, verified: 0 });
      }
      const g = out[out.length - 1];
      g.rows.push(r);
      g.populated += r.populated;
      g.verified += r.verified;
    }
    return out;
  }, [rows]);

  const totPop = rows.reduce((n, r) => n + r.populated, 0);
  const totVer = rows.reduce((n, r) => n + r.verified, 0);
  const nReviewed = rows.filter(r => statusOf(r) === "reviewed").length;

  if (err) return <div className="rdl__empty">{err}</div>;
  if (docs.length === 0) return <div className="rdl__empty">Loading overview…</div>;

  const cur = openFam ? fams.find(g => g.family === openFam) ?? null : null;

  return (
    <div className="rdl">
      <div className="rdl__head">
        <div>
          {cur ? (
            <div className="rdl__crumbs">
              <button type="button" className="rdl__crumb" onClick={() => setOpenFam(null)}>
                Documents
              </button>
              <span className="rdl__crumb-sep">›</span>
              <span className="rdl__crumb-cur">{cur.family}</span>
            </div>
          ) : (
            <h3>Documents</h3>
          )}
          <p className="rdl__sub">
            {cur ? (
              <>Click a row to verify that document field by field. Reviewing a
              document is what makes it <b>trusted</b>.</>
            ) : (
              <>Every value the AI extracted stays usable straight away. Reviewing
              a document is what makes it <b>trusted</b>. Open a folder to review
              its documents.</>
            )}
          </p>
        </div>
        <div className="rdl__totals">
          <span className="rdl__tot-n">{nReviewed}/{rows.length}</span>
          <span className="rdl__tot-l">documents fully reviewed</span>
          <span className="rdl__tot-s">{totVer} of {totPop} extracted values verified</span>
        </div>
      </div>

      <div className="rdl__scroll">
        {cur === null ? (
          <table className="rdl__table">
            <thead>
              <tr>
                <th className="rdl__c-doc">Folder</th>
                <th className="rdl__c-num">Documents</th>
                <th className="rdl__c-num">Values</th>
                <th className="rdl__c-prog">Verified</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {fams.map(g => {
                const st = statusOf(g);
                const pct = g.populated ? Math.round((g.verified / g.populated) * 100) : 0;
                return (
                  <tr
                    key={g.family}
                    className="rdl__row"
                    onClick={() => setOpenFam(g.family)}
                    title="Open this folder"
                  >
                    <td className="rdl__doc">{g.family}</td>
                    <td className="rdl__num">{g.rows.length}</td>
                    <td className="rdl__num">{g.populated || "—"}</td>
                    <td>
                      {g.populated > 0 ? (
                        <span className="rdl__prog">
                          <span className="rdl__bar"><i style={{ width: `${pct}%` }} /></span>
                          <span className="rdl__pct">{g.verified}/{g.populated}</span>
                        </span>
                      ) : <span className="rdl__na">—</span>}
                    </td>
                    <td><span className={`rdl__pill rdl__pill--${st}`}>{STATUS_TEXT[st]}</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <table className="rdl__table">
            <thead>
              <tr>
                <th className="rdl__c-doc">Document</th>
                <th>Type</th>
                <th>Date</th>
                <th className="rdl__c-num">Values</th>
                <th className="rdl__c-prog">Verified</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {cur.rows.map(r => {
                const d = r.doc;
                const st = statusOf(r);
                const pct = r.populated ? Math.round((r.verified / r.populated) * 100) : 0;
                return (
                  <tr
                    key={d.doc_id}
                    className="rdl__row"
                    onClick={() => onPick(d.doc_id)}
                    title="Open this document in review"
                  >
                    <td className="rdl__doc">{cleanTitle(d.title)}</td>
                    <td className="rdl__type">{shortType(d.doc_type) ?? "—"}</td>
                    <td className="rdl__date">{shortDate(d.doc_date_iso) ?? "—"}</td>
                    <td className="rdl__num">{r.populated || "—"}</td>
                    <td>
                      {r.populated > 0 ? (
                        <span className="rdl__prog">
                          <span className="rdl__bar"><i style={{ width: `${pct}%` }} /></span>
                          <span className="rdl__pct">{r.verified}/{r.populated}</span>
                        </span>
                      ) : <span className="rdl__na">—</span>}
                    </td>
                    <td><span className={`rdl__pill rdl__pill--${st}`}>{STATUS_TEXT[st]}</span></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
