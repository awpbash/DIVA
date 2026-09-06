import { useEffect, useMemo, useState } from "react";
import {
  KmAggregate, KmAlignedView, KmFamily, KmField, KmStatement,
  downloadKmExport, getKmAggregate, getKmFamilies, getKmFamily, kmPageUrl,
} from "../api";
import { AuthedImage } from "./AuthedImage";
import { cleanTitle } from "../docmeta";
import { loadUi, saveUi } from "../uiState";
import "./KnowledgeView.css";
import { useDocNoun } from "../branding";

// The Knowledge Base view — the aligned KM layer made tangible. Per contract FAMILY it
// shows the canonical parties (resolved across every document), the AMENDS/SUPERSEDES/
// NOVATES document chain, and — the point — each field's CURRENT value with its superseded
// history, a trust badge (human-verified vs AI-extracted), and clause evidence. A Σ button
// runs a deterministic cross-document sum (exact arithmetic, not an LLM guess).

const REL_LABEL: Record<string, string> = { AMENDS: "amends", SUPERSEDES: "supersedes", NOVATES: "novates" };

interface Rect { page_no: number; bbox: [number, number, number, number]; }
function parseRects(rects: string | null): Rect[] {
  if (!rects) return [];
  try { const a = JSON.parse(rects); return Array.isArray(a) ? (a as Rect[]) : []; } catch { return []; }
}

export function KnowledgeView() {
  const noun = useDocNoun();
  const [families, setFamilies] = useState<KmFamily[]>([]);
  // The selected family survives tab switches via the session ui hint.
  const [group, setGroup] = useState<string | null>(() => loadUi<string | null>("km.group", null));
  const [view, setView] = useState<KmAlignedView | null>(null);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [agg, setAgg] = useState<Record<string, KmAggregate>>({});
  const [err, setErr] = useState<string | null>(null);
  // Free-text filter over the aligned fields (title or value). Scales the one
  // long field list down to the rows the reader is actually after.
  const [fieldQ, setFieldQ] = useState("");
  const [exporting, setExporting] = useState(false);
  const [evidenceOpen, setEvidenceOpen] = useState(false);

  const doExport = async () => {
    if (exporting) return;
    setExporting(true);
    try { await downloadKmExport(group ?? undefined); }
    catch { /* surfaced by the button returning to idle */ }
    finally { setExporting(false); }
  };

  useEffect(() => {
    getKmFamilies()
      .then(f => {
        setFamilies(f);
        setGroup(prev => (prev && f.some(x => x.group === prev)) ? prev : f[0]?.group ?? null);
      })
      .catch(() => setErr(`Nothing here yet. Once ${noun.many} are uploaded and extracted, they show up here.`));
  }, []);

  useEffect(() => {
    if (!group) { setView(null); return; }
    saveUi("km.group", group);
    setActiveKey(null); setAgg({}); setFieldQ(""); setEvidenceOpen(false);
    getKmFamily(group).then(setView).catch(() => setErr("Couldn't load this family."));
  }, [group]);

  const needle = fieldQ.trim().toLowerCase();
  const fieldMatch = (f: KmField): boolean =>
    !needle
    || f.title.toLowerCase().includes(needle)
    || [...f.current, ...f.superseded].some(s => String(s.value ?? "").toLowerCase().includes(needle));

  const family = useMemo(() => families.find(f => f.group === group) ?? null, [families, group]);
  const docTitle = useMemo(() => {
    const m: Record<string, string> = {};
    family?.docs.forEach(d => { m[d.doc_id] = cleanTitle(d.title); });
    return m;
  }, [family]);

  async function runAggregate(f: KmField) {
    if (agg[f.field_key]) { setAgg(a => { const c = { ...a }; delete c[f.field_key]; return c; }); return; }
    try {
      const res = await getKmAggregate(f.field_key, group ?? undefined);
      setAgg(a => ({ ...a, [f.field_key]: res }));
    } catch { /* ignore — surfaced by the button staying idle */ }
  }

  return (
    <div className="kb">
      <div className="kb__bar">
        <select className="kb__fam" value={group ?? ""} onChange={e => setGroup(e.target.value)}>
          {families.map(f => (
            <option key={f.group} value={f.group}>{cleanTitle(f.title)} ({f.n_docs} docs · {f.n_fields} fields)</option>
          ))}
        </select>
        {view && (
          <input
            className="kb__filter"
            placeholder="Filter fields…"
            value={fieldQ}
            onChange={e => setFieldQ(e.target.value)}
            title="Show only fields whose name or value matches"
          />
        )}
        {view && (
          <span className="kb__counts">
            {view.n_fields} fields · <b>{view.n_verified}</b> verified
            {view.hidden_fields > 0 && <em> · {view.hidden_fields} hidden</em>}
          </span>
        )}
        {view && (
          <button
            className="kb__export"
            onClick={doExport}
            disabled={exporting}
            title="Download this family's aligned fields as an Excel workbook (current values, trust, sources, history)"
          >
            {exporting ? "Exporting…" : "⬇ Export Excel"}
          </button>
        )}
        {view && (
          <button
            type="button"
            className="kb__evidence-toggle"
            onClick={() => setEvidenceOpen(true)}
            disabled={!activeKey}
          >
            Evidence
          </button>
        )}
      </div>

      {err && !view ? (
        <div className="kb__empty">{err}</div>
      ) : !view ? (
        <div className="skel-wrap" aria-hidden>
          <div className="skel" style={{ height: 20, width: "30%" }} />
          <div className="skel" style={{ height: 56 }} />
          <div className="skel" style={{ height: 20, width: "38%" }} />
          <div className="skel" style={{ height: 90 }} />
          <div className="skel" style={{ height: 20, width: "26%" }} />
          <div className="skel" style={{ height: 120 }} />
          <div className="skel" style={{ height: 120 }} />
        </div>
      ) : (
        <div className="kb__body">
          {/* Canonical parties — resolved across the whole family. A novation
              flips which customer is CURRENT; the old one stays as history. */}
          <section className="kb__parties">
            <h3>Parties <span className="kb__hint">matched across all documents</span></h3>
            <div className="kb__party-row">
              {view.parties.length === 0 && <span className="kb__na">No parties identified yet.</span>}
              {view.parties.map(p => (
                <div key={p.key} className={`kb__party${p.current === false ? " kb__party--former" : ""}`}>
                  <span className="kb__party-role">
                    {p.roles.join(" / ")}
                    {p.current === true && <em className="kb__party-cur"> · current</em>}
                    {p.current === false && <em className="kb__party-old"> · former</em>}
                  </span>
                  <span className="kb__party-name">
                    {p.name}
                  </span>
                </div>
              ))}
            </div>
            {(view.conflicts ?? []).length > 0 && (
              <div className="kb__conflicts">
                {(view.conflicts ?? []).map((c, i) => (
                  <div key={i} className="kb__conflict">
                    ⚠ {c.reason} <em>({cleanTitle(c.doc_title)})</em>
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* Document family DAG — the declared supersedence chain. */}
          <section className="kb__chain">
            <h3>Document family <span className="kb__hint">which document amends which</span></h3>
            <div className="kb__docs">
              {(family?.docs ?? []).map(d => (
                <span key={d.doc_id} className={`kb__doc${activeStmtDoc(view, activeKey) === d.doc_id ? " is-cited" : ""}`}>
                  {cleanTitle(d.title)}{d.date && <em>{d.date}</em>}
                </span>
              ))}
            </div>
            {view.dag.length > 0 && (
              <div className="kb__edges">
                {view.dag.map((e, i) => (
                  <div key={i} className="kb__edge">
                    <b>{docTitle[e.src] ?? e.src}</b>
                    <span className={`kb__rel kb__rel--${e.rel}`}>{REL_LABEL[e.rel] ?? e.rel}</span>
                    <b>{docTitle[e.tgt] ?? e.tgt}</b>
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* Aligned fields, by category. */}
          <div className="kb__grid">
            <div className="kb__fields">
              {needle && view.categories.every(cat => !cat.fields.some(fieldMatch)) && (
                <div className="kb__ev-empty">No fields match “{fieldQ}”.</div>
              )}
              {view.categories.map(cat => {
                const shown = needle ? cat.fields.filter(fieldMatch) : cat.fields;
                if (shown.length === 0) return null;
                return (
                <div key={cat.category} className="kb__cat">
                  <div className="kb__cat-h">{cat.category.replace(/_/g, " ")}</div>
                  {shown.map(f => (
                    <FieldCard
                      key={f.full_key} f={f} active={activeKey === f.full_key}
                      agg={agg[f.field_key]}
                      onSelect={() => {
                        const next = activeKey === f.full_key ? null : f.full_key;
                        setActiveKey(next);
                        setEvidenceOpen(next !== null);
                      }}
                      onAggregate={() => runAggregate(f)}
                    />
                  ))}
                </div>
                );
              })}
            </div>

            {/* Evidence — the Prime-Directive highlight for the selected field. */}
            {evidenceOpen && (
              <button
                type="button"
                className="kb__evidence-scrim"
                onClick={() => setEvidenceOpen(false)}
                aria-label="Close evidence"
              />
            )}
            <aside className={`kb__evidence${evidenceOpen ? " kb__evidence--open" : ""}`}>
              <div className="kb__evidence-mobile-head">
                <div>
                  <strong>Source evidence</strong>
                  <span>Check the clause behind the selected field</span>
                </div>
                <button type="button" onClick={() => setEvidenceOpen(false)}>Close</button>
              </div>
              <EvidencePane view={view} activeKey={activeKey} />
            </aside>
          </div>
        </div>
      )}
    </div>
  );
}

function activeStmtDoc(view: KmAlignedView, key: string | null): string | null {
  if (!key) return null;
  for (const c of view.categories) for (const f of c.fields)
    if (f.full_key === key) return f.current[0]?.doc_id ?? null;
  return null;
}

function TrustBadge({ s }: { s: KmStatement }) {
  if (s.trust === "human_validated") {
    const pct = s.confidence != null ? ` · ${Math.round(s.confidence * 100)}%` : "";
    const votes = s.n_votes ? ` (${(s.verifiers ?? []).length}/${s.n_votes})` : "";
    const who = (s.verifiers ?? []).join(", ") || "a reviewer";
    return (
      <span className="kb__trust kb__trust--human"
            title={`Verified by ${who} in ${cleanTitle(s.doc_title)}`}>
        ✓ verified{pct}{votes}
      </span>
    );
  }
  if (s.disputed) {
    return <span className="kb__trust kb__trust--disputed" title="Verifiers disagree on this value">⚠ disputed</span>;
  }
  return <span className="kb__trust kb__trust--machine" title="AI-extracted, pending verification">AI</span>;
}

/** Collapse statements that say the same thing (case/punctuation aside) into one
 * row with a document count — five documents naming the same customer is ONE fact
 * stated five times, and should read that way. Prefers a human-verified copy. */
function dedupeStatements(stmts: KmStatement[]): { s: KmStatement; n: number }[] {
  const seen = new Map<string, { s: KmStatement; n: number }>();
  for (const s of stmts) {
    const k = String(s.value ?? "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    const hit = seen.get(k);
    if (hit) {
      hit.n += 1;
      if (s.trust === "human_validated" && hit.s.trust !== "human_validated") hit.s = s;
    } else {
      seen.set(k, { s, n: 1 });
    }
  }
  return [...seen.values()];
}

function FieldCard(props: {
  f: KmField; active: boolean; agg?: KmAggregate; onSelect: () => void; onAggregate: () => void;
}) {
  const { f } = props;
  const numeric = f.type === "value" || f.type === "number";
  const conf = f.sensitivity === "confidential";
  return (
    <div className={`kb__field${props.active ? " is-active" : ""}`} onClick={props.onSelect}>
      <div className="kb__field-h">
        <span className="kb__field-title">{f.title}{conf && <span className="kb__lock">🔒</span>}</span>
        {numeric && f.current.length > 0 && (
          <button className="kb__agg-btn" onClick={e => { e.stopPropagation(); props.onAggregate(); }}
            title="Add up this value across every document in the family">Σ</button>
        )}
      </div>

      {dedupeStatements(f.current).map(({ s, n }, i) => (
        <div key={i} className="kb__stmt">
          <span className="kb__val">{s.value}</span>
          <TrustBadge s={s} />
          <span className="kb__src">
            {n > 1 ? `same in ${n} documents` : cleanTitle(s.doc_title)}
            {n === 1 && s.page != null && ` · p${s.page}`}
          </span>
        </div>
      ))}
      {f.current.length === 0 && <div className="kb__stmt kb__stmt--none">not stated, or hidden for your access level</div>}

      {f.superseded.length > 0 && (
        <div className="kb__superseded">
          {f.superseded.map((s, i) => (
            <div key={i} className="kb__stmt kb__stmt--old" title="Replaced by a later document">
              <span className="kb__val">{s.value}</span>
              <span className="kb__src">previously, in {cleanTitle(s.doc_title)}</span>
            </div>
          ))}
        </div>
      )}

      {props.agg && (
        <div className="kb__agg" onClick={e => e.stopPropagation()}>
          <div className="kb__agg-sum">
            Σ across family = <b>{props.agg.sum?.toLocaleString()}</b>
            <span className="kb__hint"> ({props.agg.n_numbers} values from {props.agg.n_docs} docs)</span>
          </div>
          {props.agg.items.map((it, i) => (
            <div key={i} className="kb__agg-item"><span>{cleanTitle(it.title)}</span><span>{it.value}</span></div>
          ))}
        </div>
      )}
    </div>
  );
}

// Inline page image + highlight for the active field's evidence (same machinery as review).
function EvidencePane({ view, activeKey }: { view: KmAlignedView; activeKey: string | null }) {
  const field = useMemo(() => {
    if (!activeKey) return null;
    for (const c of view.categories) for (const f of c.fields) if (f.full_key === activeKey) return f;
    return null;
  }, [view, activeKey]);

  const stmt = field?.current[0] ?? field?.superseded[0] ?? null;
  const rects = parseRects(stmt?.rects ?? null);
  const page = rects[0]?.page_no ?? stmt?.page ?? null;

  if (!field) return <div className="kb__ev-empty">Select a field to see its clause evidence.</div>;
  return (
    <div className="kb__ev">
      <div className="kb__ev-title">{field.title}</div>
      {stmt?.snippet && <div className="kb__ev-snip">“{stmt.snippet}”</div>}
      {stmt && page != null && rects.length > 0 ? (
        <div className="kb__ev-page">
          <AuthedImage src={kmPageUrl(stmt.doc_id, page)} alt={`page ${page}`} />
          {rects.filter(r => r.page_no === page).map((r, i) => {
            const [x0, y0, x1, y1] = r.bbox;
            return <div key={i} className="kb__ev-hl" style={{
              left: `${x0 * 100}%`, top: `${y0 * 100}%`, width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`,
            }} />;
          })}
        </div>
      ) : (
        <div className="kb__ev-note">No highlight available for this value{stmt ? "" : " (not stated)"}.</div>
      )}
    </div>
  );
}
