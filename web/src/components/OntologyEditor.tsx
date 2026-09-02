import { useEffect, useId, useMemo, useState } from "react";
import {
  EditFieldBody, NewFieldBody, OntologyCategory, OntologyField, OntologyResponse,
} from "../api";
import "./OntologyEditor.css";

// The schema editor: a category rail on the left (with per-category sensitivity), a
// searchable field table on the right (sticky header, full-height scroll), inline
// add/edit forms, and who-edited-what attribution. Shared by two callers that back it
// with different calls: the admin Ontology tab (authenticated /ontology/* routes,
// OntologyView.tsx) and the setup wizard's clone-and-edit step (unauthenticated
// /setup/ontology/* passthrough routes, SetupWizard.tsx) — same UI either way, because
// both sides ultimately validate and persist through the exact same backend code (see
// api/routes/setup.py's "Clone-and-edit" section). Read-only for non-admins; mutations
// are re-validated server-side regardless of which caller is used.

const USER_TYPES = ["text", "number", "value", "enum", "free_text"] as const;
const TYPE_HELP: Record<string, string> = {
  text: "Short text, like a name or reference",
  number: "A plain number",
  value: "A number with a unit (e.g. 7 °C, 1,000 RT)",
  enum: "A fixed set of choices you list",
  free_text: "A sentence or clause, quoted word for word",
};
// Plain-language display names for the technical type keys.
const TYPE_LABEL: Record<string, string> = {
  text: "text", number: "number", value: "number + unit",
  enum: "choice list", free_text: "clause", reference: "reference",
  equipment: "equipment", presence_enum: "yes / no",
};

// Same title -> key rule used for a field's own auto-key below, reused for
// category names too — one definition of "how a typed label becomes an
// identifier", not two.
const slug = (s: string): string =>
  s.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");

export interface OntologyEditorProps {
  fetchOntology: () => Promise<OntologyResponse>;
  addField: (body: NewFieldBody) => Promise<unknown>;
  editField: (category: string, key: string, body: EditFieldBody) => Promise<unknown>;
  deleteField: (category: string, key: string) => Promise<unknown>;
  setSensitivity: (category: string, level: string) => Promise<unknown>;
  /** Defaults to "Ontology" — the wizard's step uses a shorter heading. */
  title?: string;
}

export function OntologyEditor(props: OntologyEditorProps) {
  const [cats, setCats] = useState<OntologyCategory[]>([]);
  const [canEdit, setCanEdit] = useState(false);
  const [active, setActive] = useState<string | "all">("all");
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);   // full_key
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = () =>
    props.fetchOntology()
      .then(o => { setCats(o.categories); setCanEdit(o.can_edit); })
      .catch(() => setError("Couldn't load the ontology."));
  useEffect(() => { reload(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function run(fn: () => Promise<unknown>) {
    setBusy(true); setError(null);
    try { await fn(); await reload(); }
    catch (e) { setError((e as Error).message || "Something went wrong."); }
    finally { setBusy(false); }
  }

  const totalFields = cats.reduce((n, c) => n + c.fields.length, 0);
  const userFields = cats.reduce((n, c) => n + c.fields.filter(f => f.user_field).length, 0);

  // Rows for the table: active category (or all), filtered by the search box.
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    const out: { cat: OntologyCategory; f: OntologyField }[] = [];
    for (const c of cats) {
      if (active !== "all" && c.key !== active) continue;
      for (const f of c.fields) {
        if (q && !(`${f.title} ${f.key} ${f.hint} ${c.title}`.toLowerCase().includes(q))) continue;
        out.push({ cat: c, f });
      }
    }
    return out;
  }, [cats, active, query]);

  return (
    <div className="ov">
      <div className="ov__bar">
        <div>
          <h2>{props.title ?? "Ontology"}</h2>
          <span className="ov__sub">
            The fields the AI fills in for every document · {totalFields} fields
            in {cats.length} categories
            {userFields > 0 && ` · ${userFields} added here`}
            {!canEdit && " · you can view but not edit"}
          </span>
        </div>
        <input
          className="ov__search" placeholder="Search fields…"
          value={query} onChange={e => setQuery(e.target.value)}
        />
        {canEdit && (
          <button className="ov__add" onClick={() => { setAdding(a => !a); setEditing(null); }}>
            {adding ? "Cancel" : "+ Add field"}
          </button>
        )}
      </div>

      {error && <div className="ov__error">{error}</div>}

      <div className="ov__body">
        {/* Category rail */}
        <aside className="ov__rail">
          <button className={`ov__cat${active === "all" ? " is-active" : ""}`} onClick={() => setActive("all")}>
            <span>All categories</span>
            <em>{totalFields}</em>
          </button>
          {cats.map(c => (
            <div key={c.key} className={`ov__cat${active === c.key ? " is-active" : ""}`}>
              <button className="ov__cat-btn" onClick={() => setActive(c.key)} title={c.title}>
                <span>{c.title}</span>
                <em>{c.fields.length}</em>
              </button>
              {canEdit ? (
                <select
                  className={`ov__level${c.level === "confidential" ? " is-conf" : ""}`}
                  value={c.level} disabled={busy}
                  onChange={e => run(() => props.setSensitivity(c.key, e.target.value))}
                  title="Confidential categories are hidden from Default staff"
                >
                  <option value="general">general</option>
                  <option value="confidential">confidential 🔒</option>
                </select>
              ) : (
                <span className={`ov__level-pill${c.level === "confidential" ? " is-conf" : ""}`}>
                  {c.level === "confidential" ? "🔒 confidential" : "general"}
                </span>
              )}
            </div>
          ))}
        </aside>

        {/* Field table */}
        <div className="ov__main">
          {adding && canEdit && (
            <FieldForm
              mode="add" categories={cats} defaultCategory={active === "all" ? cats[0]?.key : active}
              busy={busy}
              onSubmit={b => run(async () => { await props.addField(b as NewFieldBody); setAdding(false); })}
              onCancel={() => setAdding(false)}
            />
          )}
          <div className="ov__scroll">
            <table className="ov__table">
              <thead>
                <tr>
                  <th>Field</th>
                  {active === "all" && <th>Category</th>}
                  <th>Type</th>
                  <th className="ov__th-desc">What the AI looks for</th>
                  <th>Access</th>
                  <th>Last edited</th>
                  {canEdit && <th />}
                </tr>
              </thead>
              <tbody>
                {rows.map(({ cat, f }) =>
                  editing === f.full_key ? (
                    <tr key={f.full_key} className="ov__row-edit">
                      <td colSpan={canEdit ? (active === "all" ? 7 : 6) : (active === "all" ? 6 : 5)}>
                        <FieldForm
                          mode="edit" field={f} categories={cats} busy={busy}
                          onSubmit={b => run(async () => {
                            await props.editField(cat.key, f.key, b as EditFieldBody);
                            setEditing(null);
                          })}
                          onCancel={() => setEditing(null)}
                        />
                      </td>
                    </tr>
                  ) : (
                    <tr key={f.full_key} className="ov__row">
                      <td className="ov__field">
                        <span className="ov__field-title">
                          {f.title}
                          {f.user_field && <span className="ov__yours" title="A field your team added. The AI extracts it from its description.">✎ yours</span>}
                        </span>
                        <span className="ov__field-key">{f.key}</span>
                        {f.type === "enum" && f.values.length > 0 && (
                          <span className="ov__enum">{f.values.filter(v => v !== "Not Stated").join(" · ")}</span>
                        )}
                      </td>
                      {active === "all" && <td className="ov__cat-cell">{cat.title}</td>}
                      <td><span className={`ov__type ov__type--${f.type}`}>{TYPE_LABEL[f.type] ?? f.type}</span></td>
                      <td className="ov__hint">{f.hint || <em className="ov__none">—</em>}</td>
                      <td>
                        <span className={`ov__level-pill${f.sensitivity === "confidential" ? " is-conf" : ""}`}>
                          {f.sensitivity === "confidential" ? "🔒" : "general"}
                        </span>
                      </td>
                      <td className="ov__who">
                        {f.updated_by
                          ? <span title={f.updated_at ?? ""}>{f.updated_by.split("@")[0]}</span>
                          : <em className="ov__none">—</em>}
                      </td>
                      {canEdit && (
                        <td className="ov__actions">
                          <button disabled={busy} title="Edit"
                            onClick={() => { setEditing(f.full_key); setAdding(false); }}>✎</button>
                          <button disabled={busy} title={f.user_field ? "Delete" : "Hide from the schema"}
                            onClick={() => {
                              const msg = f.user_field
                                ? `Delete the field "${f.title}"?`
                                : `Hide the base field "${f.title}"? It stops being extracted (reversible by re-adding).`;
                              if (window.confirm(msg)) run(() => props.deleteField(cat.key, f.key));
                            }}>🗑</button>
                        </td>
                      )}
                    </tr>
                  )
                )}
                {rows.length === 0 && (
                  <tr><td colSpan={7} className="ov__empty">No fields match “{query}”.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}

// Add / edit form. In "add" mode everything is entered (incl. the category); in
// "edit" mode the mechanism/type are fixed (a base field keeps its extraction
// wiring) so title / hint / enum values / sensitivity are editable.
function FieldForm(props: {
  mode: "add" | "edit"; field?: OntologyField;
  categories: OntologyCategory[]; defaultCategory?: string;
  busy: boolean; onSubmit: (b: unknown) => void; onCancel: () => void;
}) {
  const f = props.field;
  const isAdd = props.mode === "add";
  const defaultCat = props.categories.find(c => c.key === (props.defaultCategory ?? props.categories[0]?.key));
  const [categoryInput, setCategoryInput] = useState(defaultCat?.title ?? props.defaultCategory ?? "");
  const [title, setTitle] = useState(f?.title ?? "");
  const [key, setKey] = useState(f?.key ?? "");
  const [keyTouched, setKeyTouched] = useState(false);
  const [type, setType] = useState(f?.type && (USER_TYPES as readonly string[]).includes(f.type) ? f.type : "text");
  const [hint, setHint] = useState(f?.hint ?? "");
  const [values, setValues] = useState((f?.values ?? []).filter(v => v !== "Not Stated").join("\n"));
  const [sensitivity, setSensitivity] = useState(f?.sensitivity ?? "general");
  const categoryListId = useId();

  const autoKey = slug(title);
  const effKey = isAdd ? (keyTouched ? key : autoKey) : (f?.key ?? "");
  const enumType = type === "enum" || f?.type === "enum";
  const valueList = values.split(/[\n,]/).map(s => s.trim()).filter(Boolean);
  // Typing the title of an existing category reuses its key (no accidental
  // duplicate); anything else mints a new one — the backend already allows
  // this (add_field creates the category container on demand), so this is
  // only a frontend restriction being lifted, not a new capability. What
  // lets from-scratch domains work at all: there ARE no existing categories
  // to pick from on the first field.
  const matchedCategory = props.categories.find(
    c => c.title.trim().toLowerCase() === categoryInput.trim().toLowerCase());
  const category = matchedCategory?.key ?? slug(categoryInput);
  const canSubmit = isAdd ? (title.trim() && category && effKey && (!enumType || valueList.length > 0)) : true;

  function submit() {
    if (isAdd) {
      props.onSubmit({
        category, key: effKey, title: title.trim(), type,
        hint: hint.trim(), multiplicity: 1,
        values: enumType ? valueList : undefined,
      } as NewFieldBody);
    } else {
      const body: EditFieldBody = { title: title.trim(), hint: hint.trim(), sensitivity };
      if (f?.type === "enum") body.values = valueList;
      props.onSubmit(body);
    }
  }

  return (
    <div className="ov__form">
      <div className="ov__form-row">
        {isAdd && (
          <label>Category
            <input
              list={categoryListId} value={categoryInput}
              onChange={e => setCategoryInput(e.target.value)}
              placeholder="Pick or type a new one"
            />
            <datalist id={categoryListId}>
              {props.categories.map(c => <option key={c.key} value={c.title} />)}
            </datalist>
          </label>
        )}
        <label>Title
          <input value={title} onChange={e => setTitle(e.target.value)} placeholder="e.g. Payment Terms" autoFocus />
        </label>
        {isAdd && (
          <label>Key
            <input value={effKey} onChange={e => { setKey(e.target.value); setKeyTouched(true); }} placeholder="payment_terms" />
          </label>
        )}
        {isAdd ? (
          <label>Type
            <select value={type} onChange={e => setType(e.target.value)}>
              {USER_TYPES.map(t => <option key={t} value={t}>{TYPE_LABEL[t] ?? t}</option>)}
            </select>
          </label>
        ) : (
          <label>Access
            <select value={sensitivity} onChange={e => setSensitivity(e.target.value)}>
              <option value="general">general</option>
              <option value="confidential">confidential</option>
            </select>
          </label>
        )}
      </div>
      {isAdd && <div className="ov__form-help">{TYPE_HELP[type]}</div>}
      <label className="ov__form-wide">Description / hint for the AI
        <textarea value={hint} onChange={e => setHint(e.target.value)} rows={2}
          placeholder="Describe exactly what to look for, e.g. 'the number of days the customer has to pay each invoice'." />
      </label>
      {enumType && (
        <label className="ov__form-wide">Choices (one per line; "Not Stated" is added automatically)
          <textarea value={values} onChange={e => setValues(e.target.value)} rows={3} placeholder={"Monthly\nQuarterly\nAnnually"} />
        </label>
      )}
      <div className="ov__form-actions">
        <button className="ov__form-cancel" onClick={props.onCancel} disabled={props.busy}>Cancel</button>
        <button className="ov__form-save" onClick={submit} disabled={props.busy || !canSubmit}>
          {isAdd ? "Add field" : "Save"}
        </button>
      </div>
    </div>
  );
}
