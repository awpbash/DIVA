import { useEffect, useState } from "react";
import { HeatmapDoc, ReviewHeatmap as HeatmapData, getReviewHeatmap } from "../api";
import { cleanTitle } from "../docmeta";
import "./ReviewHeatmap.css";
import { useDocNoun } from "../branding";

// The review OVERVIEW — a documents × categories heatmap of what's been verified.
// Each cell: green scales with the share of a category's stated fields that a human has
// verified; amber = extracted but unchecked; grey = nothing stated there. At a glance you
// see which contracts are reviewed (and therefore reliable to reference) and where the
// gaps are. Click a cell / row to open that document in the review screen.

interface Props {
  onPick: (docId: string) => void;
}

/** Verified/populated → a colour on the amber→green scale; grey when nothing is stated. */
function cellStyle(populated: number, verified: number): React.CSSProperties {
  if (populated === 0) return { background: "var(--bg-app)", opacity: 0.4 };
  const ratio = verified / populated;                    // 0 = none checked, 1 = all checked
  // amber (unreviewed) → green (verified). Hue 40°→150°, lift lightness a touch with ratio.
  const hue = 40 + ratio * 110;
  const light = 26 + ratio * 12;
  return { background: `hsl(${hue}, 62%, ${light}%)`, color: ratio > 0.6 ? "#04120c" : "#f3ede0" };
}

export function ReviewHeatmap({ onPick }: Props) {
  const noun = useDocNoun();
  const [data, setData] = useState<HeatmapData | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    getReviewHeatmap()
      .then(setData)
      .catch(() => setErr("Couldn't load the review overview."));
  }, []);

  if (err) return <div className="hm__empty">{err}</div>;
  if (!data) return <div className="hm__empty">Loading overview…</div>;
  if (data.docs.length === 0)
    return <div className="hm__empty">Nothing to review yet. Upload a {noun.one} in the Admin tab and it will show up here once extracted.</div>;

  const totalPop = data.docs.reduce((n, d) => n + d.n_populated, 0);
  const totalVer = data.docs.reduce((n, d) => n + d.n_verified, 0);
  const pct = totalPop ? Math.round((totalVer / totalPop) * 100) : 0;

  return (
    <div className="hm">
      <div className="hm__head">
        <div>
          <h3>Review coverage</h3>
          <p className="hm__sub">
            Share of each {noun.one}'s stated fields that a human has verified.
            Greener = more reviewed (safer to reference), amber = extracted but unchecked.
          </p>
        </div>
        <div className="hm__overall">
          <span className="hm__overall-pct">{pct}%</span>
          <span className="hm__overall-lbl">{totalVer} / {totalPop} verified</span>
        </div>
      </div>

      <div className="hm__scroll">
        <table className="hm__table">
          <thead>
            <tr>
              <th className="hm__corner">Document</th>
              {data.categories.map(c => (
                <th key={c.key} className="hm__cat" title={c.title}>
                  <span>{c.title}</span>
                </th>
              ))}
              <th className="hm__cat hm__cat--total">Overall</th>
            </tr>
          </thead>
          <tbody>
            {data.docs.map(d => (
              <Row key={d.doc_id} doc={d} categories={data.categories} onPick={onPick} />
            ))}
          </tbody>
        </table>
      </div>

      <div className="hm__legend">
        <span><i style={{ background: "hsl(40,62%,28%)" }} /> unreviewed</span>
        <span><i style={{ background: "hsl(95,62%,32%)" }} /> partly</span>
        <span><i style={{ background: "hsl(150,62%,38%)" }} /> verified</span>
        <span><i style={{ background: "var(--bg-app)" }} /> not stated</span>
      </div>
    </div>
  );
}

function Row({ doc, categories, onPick }: {
  doc: HeatmapDoc; categories: { key: string; title: string }[]; onPick: (id: string) => void;
}) {
  const overallRatio = doc.n_populated ? doc.n_verified / doc.n_populated : 0;
  return (
    <tr className="hm__row" onClick={() => onPick(doc.doc_id)}>
      <td className="hm__doc" title={doc.title}>{cleanTitle(doc.title)}</td>
      {categories.map(c => {
        const cell = doc.cells[c.key];
        const pop = cell?.populated ?? 0;
        const ver = cell?.verified ?? 0;
        return (
          <td key={c.key} className="hm__cell" style={cellStyle(pop, ver)}
            title={pop ? `${c.title}: ${ver}/${pop} verified` : `${c.title}: nothing stated`}>
            {pop > 0 && <span>{ver}/{pop}</span>}
          </td>
        );
      })}
      <td className="hm__cell hm__cell--total" style={cellStyle(doc.n_populated, doc.n_verified)}
        title={`${doc.n_verified}/${doc.n_populated} verified overall`}>
        {doc.n_populated > 0 ? `${Math.round(overallRatio * 100)}%` : "—"}
      </td>
    </tr>
  );
}
