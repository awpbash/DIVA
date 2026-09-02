import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import {
  Account, ActivityItem, ActivityKind, AdminDoc, AdminJob, AdminOverview,
  FeedbackItem, FeedbackStatus, RegistryFolder,
  UploadIntake,
  UsageMetrics, UsageUser, addAccount, createRegistryFolder, editAccount,
  extractDocument, feedbackAttachmentUrl, getAccounts, getActivity,
  getAdminOverview, getDevResetStatus, getFeedback, getRegistryFolders, getUsageMetrics,
  moveDocumentFolder, patchRegistryFolder, performDevReset,
  setAccountRole, setFeedbackStatus,
  updateDocumentIntake, uploadDocument,
} from "../api";
import { useDocumentTypes } from "../branding";
import { cleanTitle } from "../docmeta";
import { AuthedImage } from "./AuthedImage";
import { useAdminJobs } from "../hooks/useAdminJobs";
import { loadUi, saveUi } from "../uiState";
import "./AdminDashboard.css";

// The admin control-centre. Upload a contract and the whole chain runs by itself
// (read, AI field extraction, review record, knowledge base), so the document lands
// in every surface at once and joins the review queue; the Extract button re-runs
// the chain (retry after a failure, or after a schema change). Plus corpus/pipeline
// status and account management. Review progress lives in the Review tab — the
// Documents table links there instead of embedding a second copy of it. Admin-only.

interface Props {
  onOpenOntology: () => void;
  /** Jump to the Review tab. With a doc id it opens that document directly. */
  onGoReview: (docId?: string) => void;
}

const ROLES = ["admin", "confidential", "default"] as const;

// Pipeline position → a plain-language label.
const STATUS_LABEL: Record<string, string> = {
  uploaded: "uploaded", read: "read", ingested: "processed",
  extracted: "extracted", in_kb: "ready",
};

const DOC_GET: Record<string, (d: AdminDoc) => unknown> = {
  title: d => cleanTitle(d.title).toLowerCase(),
  type: d => d.doc_type,
  date: d => d.doc_date,
  status: d => d.status,
  kb: d => d.kb_fields,
  reviewed: d => (d.populated ? d.verified / d.populated : -1),
};

type AdminSection = "docs" | "accounts" | "usage" | "feedback" | "activity";

// Ordered by how often an admin actually opens each: documents daily, the
// feedback inbox next (it carries the triage badge), then people and usage.
// The registry is one-time master-data setup and the activity log is an audit
// trail, so both sit at the end.
const SECTIONS: { key: AdminSection; label: string }[] = [
  { key: "docs", label: "Documents" },
  { key: "feedback", label: "Feedback" },
  { key: "accounts", label: "Accounts" },
  { key: "usage", label: "Usage" },
  { key: "activity", label: "Activity" },
];

const RELATIONS: { value: NonNullable<UploadIntake["relation"]>; label: string }[] = [
  { value: "standalone", label: "New document (standalone)" },
  { value: "amends", label: "Amends an earlier document" },
  { value: "novates", label: "Transfers an earlier document to a new party" },
  { value: "supersedes", label: "Replaces an earlier document" },
];

// Documents drill-in: the pseudo-folder for docs that sit in no folder.
const LOOSE = "__loose__";
const LOOSE_LABEL = "Loose documents";

/** Internal actor strings ("system:registry-fixup") read as a leak to
 * stakeholders — show them all as plain "system". */
const humanActor = (s: string | null | undefined): string =>
  !s ? "" : s.startsWith("system:") || s === "system" ? "system" : s;

/** One row of the top-level folder table. folder = null is the loose row. */
interface FolderRow {
  key: string; folder: RegistryFolder | null; name: string; nDocs: number;
  updatedAt: string | null; updatedBy: string | null; active: boolean;
}

const FOLD_GET: Record<string, (r: FolderRow) => unknown> = {
  name: r => r.name.toLowerCase(),
  docs: r => r.nDocs,
  edited: r => r.updatedAt,
};

// What kind of document an upload declares itself to be (required, with date).
// Comes from the active domain's schema via GET /branding, so each deployment
// offers its own vocabulary. It used to be a literal list of one domain's
// words, which offered every other deployment a menu of things it does not
// have and no way to name what it does.

export function AdminDashboard({ onOpenOntology, onGoReview }: Props) {
  const docTypeOptions = useDocumentTypes();
  const [ov, setOv] = useState<AdminOverview | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  // Jobs come from the app-level poll (useAdminJobs) — one interval for the
  // whole app, shared with the ticker strip.
  const { jobs, seed: seedJob } = useAdminJobs();
  const [err, setErr] = useState<string | null>(null);
  // Corner toast instead of a banner pushed into the page flow.
  const [toast, setToast] = useState<Toast | null>(null);
  const toastTimer = useRef<number | undefined>(undefined);
  const notify = (kind: Toast["kind"], text: string) => {
    setToast({ kind, text });
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), kind === "err" ? 8000 : 6000);
  };
  const [docQ, setDocQ] = useState("");
  // One section at a time behind sub-tabs — five stacked panels made the page
  // an endless scroll. KPIs stay above the tabs (they answer "is anything on
  // fire?" at a glance; everything deeper lives in its section). The open
  // section survives tab switches via the session ui hint.
  const [section, setSection] = useState<AdminSection>(() => loadUi<AdminSection>("admin.section", "docs"));
  useEffect(() => { saveUi("admin.section", section); }, [section]);
  const [metrics, setMetrics] = useState<UsageMetrics | null>(null);
  const [fbCounts, setFbCounts] = useState<Record<string, number>>({});
  const { sort: docSort, toggle: docToggle } = useSort();
  const { sort: foldSort, toggle: foldToggle } = useSort();
  const fileRef = useRef<HTMLInputElement>(null);

  // Upload intake: the uploader DECLARES the type, date and relationship.
  // The PDF is never trusted to carry its own identity.
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [intake, setIntake] = useState<UploadIntake>({ relation: "standalone" });
  const [folders, setFolders] = useState<RegistryFolder[]>([]);
  // The document being organised (folder, relationship, type, date).
  const [orgDoc, setOrgDoc] = useState<AdminDoc | null>(null);
  // Drill-in navigation for the Documents section: null = the folder list,
  // a folder_id = inside that folder, LOOSE = the no-folder pseudo-folder.
  // Survives tab switches. A stale id just shows an empty folder with the
  // breadcrumb back to the list.
  const [openFolder, setOpenFolderState] = useState<string | null>(() => loadUi<string | null>("admin.folder", null));
  const setOpenFolder = (v: string | null) => { setOpenFolderState(v); saveUi("admin.folder", v); };
  // Folder create/rename dialog. { folder: null } = create a new one.
  const [folderEdit, setFolderEdit] = useState<{ folder: RegistryFolder | null } | null>(null);
  // Upload dialog folder choice ("" = none, NEW_FOLDER = create on submit).
  const [upFolder, setUpFolder] = useState("");
  const [upFolderName, setUpFolderName] = useState("");

  const reloadAccounts = () => getAccounts().then(setAccounts).catch(() => {});
  const reloadOverview = () => getAdminOverview().then(setOv).catch(() => setErr("Couldn't load the dashboard."));
  const reloadMetrics = () => getUsageMetrics().then(setMetrics).catch(() => {});
  const reloadRegistry = () => {
    getRegistryFolders().then(setFolders).catch(() => {});
  };
  useEffect(() => {
    reloadOverview();
    reloadAccounts();
    reloadMetrics();
    reloadRegistry();
    // Counts only (for the KPI + tab badge); the Feedback section re-fetches
    // on open and keeps these in sync through onCounts.
    getFeedback(null).then(d => setFbCounts(d.counts)).catch(() => {});
  }, []);

  // Watch the shared job state for a running job finishing — the finished
  // extraction changes the corpus, so the overview needs a refetch.
  const prevJobs = useRef<Record<string, AdminJob>>({});
  useEffect(() => {
    const prev = prevJobs.current;
    prevJobs.current = jobs;
    const finished = Object.keys(prev).some(
      k => prev[k].status === "running" && jobs[k] && jobs[k].status !== "running");
    if (finished) reloadOverview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobs]);

  // The app-level job ticker lands here: jump to the Documents section.
  useEffect(() => {
    const onGoto = () => { setSection("docs"); setOpenFolder(null); };
    window.addEventListener("admin:goto-docs", onGoto);
    return () => window.removeEventListener("admin:goto-docs", onGoto);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const linkRelation = intake.relation === "amends" || intake.relation === "novates"
    || intake.relation === "supersedes";
  const relVerb = intake.relation === "novates" ? "novate"
    : intake.relation === "supersedes" ? "replace" : "amend";
  // The one thing still missing before Upload can go, shown inline in the dialog.
  const uploadBlocker = !uploadFile ? "Choose a PDF to upload."
    : upFolder === NEW_FOLDER && !upFolderName.trim() ? "Name the new folder."
    : !intake.document_type ? "Pick the document type."
    : !intake.document_date ? "Set the document's date."
    : linkRelation && !intake.parent_doc_id ? `Pick the contract this one will ${relVerb}.`
    : null;
  const canUpload = !uploadBlocker && !uploading;

  function onDropFile(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (!f) return;
    if (f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf")) setUploadFile(f);
    else notify("err", "Only PDF files can be uploaded.");
  }

  async function onUpload() {
    if (!uploadFile || !canUpload) return;
    setUploading(true);
    try {
      const body: UploadIntake = { ...intake };
      if (!linkRelation) delete body.parent_doc_id;
      if (body.relation === "standalone") delete body.relation;
      if (upFolder === NEW_FOLDER) {
        const created = await createRegistryFolder({ name: upFolderName.trim() });
        body.folder_id = created.folder_id;
      } else if (upFolder) {
        body.folder_id = upFolder;
      }
      const res = await uploadDocument(uploadFile, body);
      notify("ok", res.already_known
        ? (res.extracting
          ? `"${res.title}" was already in the library. Resuming its extraction.`
          : `"${res.title}" is already in the library. Your declared details were saved and the knowledge base is updating.`)
        : `"${res.title}" uploaded as document ${res.doc_id.slice(0, 12)}. Extracting it now. It will show up in Chat, Review and Knowledge in a few minutes.`);
      seedJob(res.doc_id, { status: "running", kind: res.extracting ? "extract" : "refresh", stage: "starting…" });
      reloadOverview();
      reloadRegistry();
      setUploadOpen(false);
      setUploadFile(null);
      setIntake({ relation: "standalone" });
      setUpFolder("");
      setUpFolderName("");
    } catch (e) { notify("err", (e as Error).message); }
    setUploading(false);
    if (fileRef.current) fileRef.current.value = "";
  }

  async function onExtract(docId: string) {
    try {
      await extractDocument(docId);
      seedJob(docId, { status: "running", kind: "extract", stage: "starting…" });
    } catch (e) { notify("err", (e as Error).message); }
  }

  // Computed before the early returns because usePager is a hook.
  const folderNames = useMemo(() => {
    const m = new Map<string, string>();
    for (const f of folders) m.set(f.folder_id, f.name);
    return m;
  }, [folders]);
  const docNeedle = docQ.trim().toLowerCase();
  const allDocs = ov?.documents ?? [];
  const titleHit = (d: AdminDoc) =>
    (cleanTitle(d.title) + " " + d.title).toLowerCase().includes(docNeedle);
  // A doc whose group matches no folder counts as loose, same as group = null.
  const isLoose = (d: AdminDoc) => !d.group || !folderNames.has(d.group);
  const looseDocs = allDocs.filter(isLoose);

  /** Running and failed extraction jobs rolled up per folder, so activity is
   * visible from the top-level folder list without drilling in. */
  const folderJobs = (key: string): { running: number; failed: number } => {
    const members = key === LOOSE ? looseDocs : allDocs.filter(d => d.group === key);
    let running = 0, failed = 0;
    for (const d of members) {
      const j = jobs[d.doc_id];
      if (j?.status === "running") running += 1;
      else if (j?.status === "error") failed += 1;
    }
    return { running, failed };
  };

  // Top level: one row per folder plus the loose pseudo-row, counted from the
  // live overview so the count always matches what entering the folder shows.
  const folderRows: FolderRow[] = [
    ...folders.map(f => ({
      key: f.folder_id, folder: f, name: f.name,
      nDocs: allDocs.filter(d => d.group === f.folder_id).length,
      updatedAt: f.updated_at, updatedBy: f.updated_by, active: !!f.active,
    })),
    ...(looseDocs.length ? [{
      key: LOOSE, folder: null, name: LOOSE_LABEL, nDocs: looseDocs.length,
      updatedAt: null, updatedBy: null, active: true,
    }] : []),
  ];
  const visibleFolders = sortRows(
    docNeedle ? folderRows.filter(r => r.name.toLowerCase().includes(docNeedle)) : folderRows,
    foldSort, FOLD_GET);
  const folderPager = usePager(visibleFolders, `${docNeedle}|${foldSort.key}|${foldSort.dir}`);
  // Top-level search also surfaces documents. Picking one jumps into its
  // folder with the query kept, so the match stays in view.
  const docMatches = openFolder === null && docNeedle ? allDocs.filter(titleHit) : [];

  // Inside a folder: the existing docs table scoped to its members.
  const curFolder = openFolder && openFolder !== LOOSE
    ? folders.find(f => f.folder_id === openFolder) ?? null : null;
  const folderDocs = openFolder === LOOSE ? looseDocs
    : openFolder ? allDocs.filter(d => d.group === openFolder) : [];
  const visibleDocs = sortRows(
    docNeedle ? folderDocs.filter(titleHit) : folderDocs, docSort, DOC_GET);
  const docPager = usePager(visibleDocs, `${openFolder}|${docNeedle}|${docSort.key}|${docSort.dir}`);

  /** Open the upload dialog pre-filed into a folder. */
  function uploadIntoFolder(f: RegistryFolder) {
    setUpFolder(f.folder_id);
    setUpFolderName("");
    setIntake({ relation: "standalone" });
    setUploadOpen(true);
  }

  /** Folder picked in the upload dialog: drop a parent pick that is not in the
   * folder (the dropdown filters to folder members). */
  function pickUploadFolder(v: string) {
    setUpFolder(v);
    const f = folders.find(x => x.folder_id === v);
    if (!f) return;
    setIntake(i => ({
      ...i,
      parent_doc_id: i.parent_doc_id
        && allDocs.some(d => d.doc_id === i.parent_doc_id && d.group === v)
        ? i.parent_doc_id : undefined,
    }));
  }

  async function toggleFolderActive(f: RegistryFolder) {
    try {
      await patchRegistryFolder(f.folder_id, { active: !f.active });
      notify("ok", `Folder ${f.active ? "retired" : "restored"}. Its documents keep their family either way.`);
      reloadRegistry();
    } catch (e) { notify("err", (e as Error).message); }
  }

  if (err) return <div className="adm__empty">{err}</div>;
  if (!ov) return <div className="adm__empty">Loading dashboard…</div>;

  const t = ov.totals;
  const pctVerified = t.populated ? Math.round((t.verified / t.populated) * 100) : 0;
  const q14 = metrics ? metrics.daily.reduce((n, d) => n + d.questions, 0) : null;
  const tok14 = metrics ? metrics.daily.reduce((n, d) => n + d.tokens, 0) : 0;

  return (
    <div className="adm">
      <div className="adm__head">
        <h2>Admin dashboard</h2>
        <div className="adm__actions">
          <button onClick={onOpenOntology} title="Fields + category sensitivity">🗂 Manage ontology</button>
          <button className="adm__upload" onClick={() => setUploadOpen(true)}
            title="Upload a contract PDF and declare its type, date and relationship. It is read and extracted automatically (uses AI credits), then queued for review.">
            ⬆ Upload document
          </button>
          <input ref={fileRef} type="file" accept="application/pdf" style={{ display: "none" }}
            onChange={e => setUploadFile(e.target.files?.[0] ?? null)} />
        </div>
      </div>

      {uploadOpen && (
        <Modal
          title="Upload a contract"
          onClose={() => { if (!uploading) setUploadOpen(false); }}
          footer={
            <>
              {uploadBlocker && <span className="modal__blocker">{uploadBlocker}</span>}
              <button className="modal__cancel" disabled={uploading}
                onClick={() => setUploadOpen(false)}>Cancel</button>
              <button className="modal__go" disabled={!canUpload} onClick={onUpload}>
                {uploading ? "Uploading…" : "Upload + extract"}
              </button>
            </>
          }
        >
          <p className="modal__hint">
            Declare what you know about this document. Human input wins: the AI
            only cross-checks it.
          </p>
          <div
            className={`updrop${dragOver ? " is-over" : ""}${uploadFile ? " has-file" : ""}`}
            role="button"
            tabIndex={0}
            onClick={() => fileRef.current?.click()}
            onKeyDown={e => { if (e.key === "Enter" || e.key === " ") fileRef.current?.click(); }}
            onDragOver={e => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={onDropFile}
          >
            {uploadFile ? (
              <span className="updrop__file">
                <span className="updrop__name" title={uploadFile.name}>{uploadFile.name}</span>
                <span className="updrop__size">{fmtMB(uploadFile.size)}</span>
                <button type="button" className="updrop__clear" aria-label="Remove file"
                  onClick={e => {
                    e.stopPropagation();
                    setUploadFile(null);
                    if (fileRef.current) fileRef.current.value = "";
                  }}>
                  ✕
                </button>
              </span>
            ) : (
              <span>Drop a PDF here, or click to browse</span>
            )}
          </div>
          <div className="modal__field">
            <label>Folder</label>
            <select value={upFolder} onChange={e => pickUploadFolder(e.target.value)}>
              <option value="">No folder (standalone)</option>
              {folders.filter(f => f.active).map(f => (
                <option key={f.folder_id} value={f.folder_id}>{f.name}</option>
              ))}
              <option value={NEW_FOLDER}>+ New folder…</option>
            </select>
          </div>
          {upFolder === NEW_FOLDER && (
            <div className="modal__field">
              <label>New folder name</label>
              <input value={upFolderName} placeholder="e.g. Northwind Logistics master agreement"
                onChange={e => setUpFolderName(e.target.value)} autoFocus />
            </div>
          )}
          <div className="modal__grid2">
            <div className="modal__field">
              <label>Document type</label>
              <select value={intake.document_type ?? ""}
                onChange={e => setIntake(i => ({ ...i, document_type: e.target.value || undefined }))}>
                <option value="">Pick a type…</option>
                {docTypeOptions.map(t => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
            <div className="modal__field">
              <label>Document date</label>
              <input type="date" value={intake.document_date ?? ""}
                onChange={e => setIntake(i => ({ ...i, document_date: e.target.value || undefined }))} />
            </div>
          </div>
          <div className="modal__field">
            <label>Relationship</label>
            <select value={intake.relation ?? "standalone"}
              onChange={e => setIntake(i => ({ ...i, relation: e.target.value as UploadIntake["relation"] }))}>
              {RELATIONS.map(r => <option key={r.value} value={r.value}>{r.label}</option>)}
            </select>
          </div>
          {linkRelation && (
            <div className="modal__field">
              <label>
                {intake.relation === "novates" ? "Contract it novates"
                  : intake.relation === "supersedes" ? "Contract it replaces" : "Contract it amends"}
              </label>
              <select value={intake.parent_doc_id ?? ""}
                onChange={e => setIntake(i => ({ ...i, parent_doc_id: e.target.value || undefined }))}>
                <option value="">Pick the existing contract…</option>
                {(upFolder && upFolder !== NEW_FOLDER
                  ? allDocs.filter(d => d.group === upFolder)
                  : allDocs).map(d => (
                  <option key={d.doc_id} value={d.doc_id}>{cleanTitle(d.title)}</option>
                ))}
              </select>
            </div>
          )}
        </Modal>
      )}

      {/* Only actionable KPIs live here: corpus intake, the review-progress
          accountability number, and whether people actually use the app.
          Graph internals (statement/party/family counts) belong to the
          Knowledge tab, not to admin operations. */}
      <div className="adm__stats">
        <Stat label="Documents" value={t.documents} hint={`${t.extracted}/${t.documents} extracted`} />
        <Stat label="Fields reviewed" value={`${pctVerified}%`} hint={`${t.verified}/${t.populated} filled fields`} accent />
        <Stat label="Active users" value={metrics?.totals.active_users_14d ?? "—"} hint="last 14 days" />
        <Stat label="Questions" value={q14 ?? "—"} hint={metrics ? `${fmtK(tok14)} tokens, 14 days` : "last 14 days"} />
        <Stat label="New feedback" value={fbCounts.new ?? 0} hint="awaiting triage" accent={(fbCounts.new ?? 0) > 0} />
      </div>

      <div className="adm__tabs" role="tablist">
        {SECTIONS.map(s => (
          <button
            key={s.key}
            role="tab"
            aria-selected={section === s.key}
            className={section === s.key ? "is-active" : ""}
            onClick={() => setSection(s.key)}
          >
            {s.label}
            {s.key === "feedback" && (fbCounts.new ?? 0) > 0 && (
              <span className="adm__tab-n">{fbCounts.new}</span>
            )}
          </button>
        ))}
      </div>

      <div className="adm__body">
      {section === "docs" && openFolder === null && (
        <section className="adm__panel adm__panel--wide">
          <div className="adm__panel-h">
            <h3>Documents</h3>
            <div className="adm__panel-act">
              <button className="adm__link" onClick={() => setFolderEdit({ folder: null })}>+ New folder</button>
              <button className="adm__link" onClick={() => onGoReview()}>Open review →</button>
            </div>
          </div>
          <input
            className="adm__search"
            placeholder="Search folders and documents…"
            value={docQ}
            onChange={e => setDocQ(e.target.value)}
          />
          <div className="adm__scroll">
            <table className="adm__table">
              <thead>
                <tr>
                  <SortTh k="name" sort={foldSort} onSort={foldToggle}>Folder</SortTh>
                  <SortTh k="docs" sort={foldSort} onSort={foldToggle} num>Documents</SortTh>
                  <SortTh k="edited" sort={foldSort} onSort={foldToggle}>Last edited</SortTh>
                </tr>
              </thead>
              <tbody>
                {folderPager.rows.map(r => {
                  const fj = folderJobs(r.key);
                  return (
                  <tr
                    key={r.key}
                    className={`adm__fold-row${r.active ? "" : " adm__reg-retired"}`}
                    onClick={() => setOpenFolder(r.key)}
                    title={r.folder ? "Open this folder" : "Documents not filed in any folder yet"}
                  >
                    <td className="adm__doc">
                      {r.name}
                      {!r.active && <span className="adm__pill" style={{ marginLeft: 6 }}>retired</span>}
                      {fj.running > 0 && (
                        <span className="adm__pill adm__pill--busy" style={{ marginLeft: 6 }}>
                          ⏳ extracting {fj.running > 1 ? fj.running : ""}
                        </span>
                      )}
                      {fj.failed > 0 && (
                        <span className="adm__pill adm__pill--err" style={{ marginLeft: 6 }}>
                          {fj.failed} failed
                        </span>
                      )}
                    </td>
                    <td className="adm__num">{r.nDocs || "—"}</td>
                    <td className="adm__muted">
                      {r.updatedAt
                        ? `${fmtWhen(r.updatedAt)}${r.updatedBy ? ` · ${humanActor(r.updatedBy)}` : ""}`
                        : "—"}
                    </td>
                  </tr>
                  );
                })}
                {folderPager.total === 0 && docMatches.length === 0 && (
                  <tr><td colSpan={5} className="adm__muted" style={{ padding: 14, textAlign: "center" }}>
                    {docNeedle ? `Nothing matches “${docQ}”.` : "No documents yet. Upload one above."}
                  </td></tr>
                )}
              </tbody>
            </table>
            {docMatches.length > 0 && (
              <>
                <div className="adm__reg-h">
                  <h4>Matching documents <span className="adm__count">{docMatches.length}</span></h4>
                </div>
                <table className="adm__table">
                  <thead><tr><th>Document</th><th>Folder</th></tr></thead>
                  <tbody>
                    {docMatches.map(d => (
                      <tr
                        key={d.doc_id}
                        className="adm__fold-row"
                        onClick={() => setOpenFolder(isLoose(d) ? LOOSE : d.group!)}
                        title="Open this document's folder"
                      >
                        <td className="adm__doc" title={d.title}>{cleanTitle(d.title)}</td>
                        <td className="adm__muted">
                          {isLoose(d) ? LOOSE_LABEL : folderNames.get(d.group!) ?? d.group}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </div>
          <Pager p={folderPager} />
        </section>
      )}

      {section === "docs" && openFolder !== null && (
        <section className="adm__panel adm__panel--wide">
          <div className="adm__panel-h">
            <div className="adm__crumbs">
              <button className="adm__crumb" onClick={() => setOpenFolder(null)}>Documents</button>
              <span className="adm__crumb-sep">›</span>
              <span className="adm__crumb-cur">
                {openFolder === LOOSE ? LOOSE_LABEL : curFolder?.name ?? openFolder}
              </span>
              {curFolder && !curFolder.active && (
                <span className="adm__pill">retired</span>
              )}
            </div>
            {curFolder && (
              <div className="adm__panel-act">
                <button className="adm__link" onClick={() => setFolderEdit({ folder: curFolder })}>Rename</button>
                <button className="adm__link" onClick={() => toggleFolderActive(curFolder)}>
                  {curFolder.active ? "Retire" : "Restore"}
                </button>
                <button className="adm__link" onClick={() => uploadIntoFolder(curFolder)}>
                  Upload into this folder
                </button>
              </div>
            )}
          </div>
          <input
            className="adm__search"
            placeholder="Search documents in this folder…"
            value={docQ}
            onChange={e => setDocQ(e.target.value)}
          />
          <div className="adm__scroll">
            <table className="adm__table">
              <thead>
                <tr>
                  <SortTh k="title" sort={docSort} onSort={docToggle}>Document</SortTh>
                  <SortTh k="type" sort={docSort} onSort={docToggle}>Type</SortTh>
                  <SortTh k="date" sort={docSort} onSort={docToggle}>Date</SortTh>
                  <SortTh k="status" sort={docSort} onSort={docToggle}>Status</SortTh>
                  <SortTh k="kb" sort={docSort} onSort={docToggle} num>KB fields</SortTh>
                  <SortTh k="reviewed" sort={docSort} onSort={docToggle}>Reviewed</SortTh>
                  <th />
                </tr>
              </thead>
              <tbody>
                {docPager.rows.map(d => {
                  const job = jobs[d.doc_id];
                  const running = job?.status === "running";
                  return (
                    <tr key={d.doc_id}>
                      <td className="adm__doc" title={d.title}>{cleanTitle(d.title)}</td>
                      <td className="adm__muted">{d.doc_type || "—"}</td>
                      <td className="adm__muted" style={{ whiteSpace: "nowrap" }}>{d.doc_date || "—"}</td>
                      <td>
                        {running ? (
                          <span className="adm__pill adm__pill--busy" title={job.stage}>⏳ {job.stage}</span>
                        ) : job?.status === "error" ? (
                          <span className="adm__pill adm__pill--err" title={job.error ?? ""}>failed, click Extract to retry</span>
                        ) : (
                          <span className={`adm__pill${d.status === "in_kb" ? " adm__pill--ok" : ""}`}>
                            {STATUS_LABEL[d.status] ?? d.status}
                          </span>
                        )}
                      </td>
                      <td className="adm__num">{d.kb_fields || "—"}</td>
                      <td>
                        {d.populated === 0 ? <span className="adm__muted">—</span> : (
                          <span className="adm__rev">
                            <span className="adm__rev-bar"><i style={{ width: `${(d.verified / d.populated) * 100}%` }} /></span>
                            {d.verified}/{d.populated}
                          </span>
                        )}
                      </td>
                      <td className="adm__reg-act">
                        {d.populated > 0 && (
                          <button className="adm__link" onClick={() => onGoReview(d.doc_id)}
                            title="Open this document in the Review screen, field by field">
                            Review
                          </button>
                        )}
                        <button className="adm__link" onClick={() => setOrgDoc(d)}
                          title="Folder, relationship, document type and date. Declared details, no AI credits.">
                          Organise
                        </button>
                        {!running && d.status !== "in_kb" && (
                          <button className="adm__extract" onClick={() => onExtract(d.doc_id)}
                            title="Run the AI extraction for this document (uses AI credits). Safe to re-run: finished steps are reused.">
                            ⚡ Extract
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
                {docPager.total === 0 && (
                  <tr><td colSpan={7} className="adm__muted" style={{ padding: 14, textAlign: "center" }}>
                    {docNeedle
                      ? `No documents match “${docQ}”.`
                      : "This folder has no documents yet."}
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
          <Pager p={docPager} />
        </section>
      )}

      {section === "accounts" && (
        <section className="adm__panel adm__panel--wide">
          <AccountManager accounts={accounts} onChanged={reloadAccounts} />
          <DangerZone />
        </section>
      )}

      {section === "usage" && (
        <section className="adm__panel adm__panel--wide">
          <UsagePanel metrics={metrics} onRefresh={reloadMetrics} />
        </section>
      )}

      {section === "feedback" && (
        <section className="adm__panel adm__panel--wide">
          <FeedbackPanel onCounts={setFbCounts} />
        </section>
      )}

      {section === "activity" && (
        <section className="adm__panel adm__panel--wide">
          <ActivityPanel />
        </section>
      )}
      </div>

      {orgDoc && (
        <OrganiseModal
          doc={orgDoc}
          docs={allDocs}
          folders={folders}
          notify={notify}
          onClose={() => setOrgDoc(null)}
          onSaved={() => { reloadOverview(); reloadRegistry(); }}
        />
      )}

      {folderEdit && (
        <FolderEditModal
          folder={folderEdit.folder}
          notify={notify}
          onClose={() => setFolderEdit(null)}
          onSaved={reloadRegistry}
        />
      )}

      {toast && (
        <div className={`toast${toast.kind === "err" ? " toast--err" : ""}`} role="status">
          <span className="toast__text">{toast.text}</span>
          <button className="toast__x" aria-label="Dismiss" onClick={() => setToast(null)}>✕</button>
        </div>
      )}
    </div>
  );
}

// ---- Danger zone: wipe this instance back to a fresh install -----------------
// Hidden entirely unless the operator opted in with VERBATIM_ALLOW_RESET=1 (see
// api/routes/admin.py) — a real deployment must not ship a live self-destruct
// button by default. Calls the same perform_reset() the CLI
// (python -m scripts.reset_dev) uses, then restarts into the setup wizard, same
// as a first-run install.
function DangerZone() {
  const [enabled, setEnabled] = useState(false);
  const [phrase, setPhrase] = useState("RESET");
  const [open, setOpen] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [wipeKey, setWipeKey] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getDevResetStatus()
      .then(s => { setEnabled(s.enabled); setPhrase(s.confirm_phrase); })
      .catch(() => setEnabled(false));
  }, []);

  if (!enabled) return null;

  async function doReset() {
    setBusy(true); setError(null);
    try {
      await performDevReset(confirmText, wipeKey);
      // The process is restarting; there is no session left to keep working
      // in. Say so plainly rather than pretending the dashboard still works.
      setDone(true);
    } catch (e) { setError((e as Error).message); setBusy(false); }
  }

  if (done) {
    return (
      <div className="adm__danger">
        <h3>Danger zone</h3>
        <p>Resetting… this instance is restarting as a fresh install. Reload this page in a few seconds.</p>
      </div>
    );
  }

  return (
    <div className="adm__danger">
      <h3>Danger zone</h3>
      <p>
        Wipe every account, document and extracted fact on this instance and
        restart it as if freshly installed — the setup wizard runs again on
        the next load. This cannot be undone.
      </p>
      {!open ? (
        <button className="adm__danger-btn" onClick={() => setOpen(true)}>Reset this instance…</button>
      ) : (
        <div className="adm__danger-confirm">
          <label className="adm__danger-check">
            <input type="checkbox" checked={wipeKey} onChange={e => setWipeKey(e.target.checked)} />
            Also forget the configured model API key
          </label>
          <label>
            Type <code>{phrase}</code> to confirm
            <input value={confirmText} onChange={e => setConfirmText(e.target.value)} autoFocus />
          </label>
          {error && <div className="adm__err">{error}</div>}
          <div className="adm__danger-actions">
            <button onClick={() => { setOpen(false); setConfirmText(""); setError(null); }} disabled={busy}>
              Cancel
            </button>
            <button
              className="adm__danger-btn" disabled={busy || confirmText.trim().toUpperCase() !== phrase}
              onClick={doReset}
            >
              {busy ? "Resetting…" : "Wipe and restart"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---- Organise a document (folder + declared relationship, after upload) ------
// Everything here is DECLARED detail: it lands on the sidecar and the registry
// through the same paths the upload dialog uses, then the knowledge base
// refreshes for free. No AI credits are spent from this dialog.
const NEW_FOLDER = "__new__";

function OrganiseModal({ doc, docs, folders, notify, onClose, onSaved }: {
  doc: AdminDoc; docs: AdminDoc[]; folders: RegistryFolder[];
  notify: (kind: Toast["kind"], text: string) => void;
  onClose: () => void; onSaved: () => void;
}) {
  const docTypeOptions = useDocumentTypes();
  const knownFolder = folders.some(f => f.folder_id === (doc.group ?? ""));
  const [folderSel, setFolderSel] = useState<string>(doc.group ?? "");
  const [newName, setNewName] = useState("");
  const [relation, setRelation] = useState<NonNullable<UploadIntake["relation"]>>(
    (doc.relation as UploadIntake["relation"]) ?? "standalone");
  const [parentId, setParentId] = useState(doc.parent_doc_id ?? "");
  const [docType, setDocType] = useState(doc.doc_type ?? "");
  const [docDate, setDocDate] = useState(doc.doc_date ?? "");
  const [saving, setSaving] = useState(false);

  const linkRel = relation === "amends" || relation === "novates" || relation === "supersedes";
  const targetFolder = folderSel === NEW_FOLDER ? null : (folderSel || null);

  // Parent picker: documents already in the target folder first, then the rest.
  const parentDocs = useMemo(() => {
    const inFolder = (x: AdminDoc) => (targetFolder && (x.group ?? "") === targetFolder ? 0 : 1);
    return docs
      .filter(x => x.doc_id !== doc.doc_id)
      .sort((a, b) => inFolder(a) - inFolder(b)
        || cleanTitle(a.title).localeCompare(cleanTitle(b.title)));
  }, [docs, doc.doc_id, targetFolder]);

  // Only what actually changed goes to the server.
  const intakePatch: UploadIntake = {};
  if (relation !== ((doc.relation as UploadIntake["relation"]) ?? "standalone")) intakePatch.relation = relation;
  if (linkRel && parentId && parentId !== (doc.parent_doc_id ?? "")) intakePatch.parent_doc_id = parentId;
  if (docType && docType !== (doc.doc_type ?? "")) intakePatch.document_type = docType;
  if (docDate && docDate !== (doc.doc_date ?? "")) intakePatch.document_date = docDate;
  const hasIntakeChange = Object.keys(intakePatch).length > 0;
  const folderChanged = folderSel === NEW_FOLDER
    || (targetFolder ?? "") !== (doc.group ?? "");

  const blocker = folderSel === NEW_FOLDER && !newName.trim() ? "Name the new folder."
    : linkRel && !parentId ? "Pick the contract this one relates to."
    : hasIntakeChange && !docType ? "Pick the document type."
    : hasIntakeChange && !docDate ? "Set the document's date."
    : !folderChanged && !hasIntakeChange ? "Nothing changed yet."
    : null;

  async function save() {
    if (blocker || saving) return;
    setSaving(true);
    try {
      let fid = targetFolder;
      if (folderSel === NEW_FOLDER) {
        const created = await createRegistryFolder({ name: newName.trim() });
        fid = created.folder_id;
      }
      if (folderSel === NEW_FOLDER || (fid ?? "") !== (doc.group ?? "")) {
        await moveDocumentFolder(doc.doc_id, fid);
      }
      if (hasIntakeChange) {
        // Type and date ride along when set: the merged declaration must
        // always carry them, even if the document never declared any.
        if (docType) intakePatch.document_type = docType;
        if (docDate) intakePatch.document_date = docDate;
        await updateDocumentIntake(doc.doc_id, intakePatch);
      }
      notify("ok", `"${cleanTitle(doc.title)}" updated. The knowledge base is refreshing.`);
      onSaved();
      onClose();
    } catch (e) { notify("err", (e as Error).message); }
    setSaving(false);
  }

  return (
    <Modal
      title={`Organise ${cleanTitle(doc.title)}`}
      onClose={() => { if (!saving) onClose(); }}
      footer={
        <>
          {blocker && <span className="modal__blocker">{blocker}</span>}
          <button className="modal__cancel" disabled={saving} onClick={onClose}>Cancel</button>
          <button className="modal__go" disabled={!!blocker || saving} onClick={save}>
            {saving ? "Saving…" : "Save"}
          </button>
        </>
      }
    >
      <p className="modal__hint">
        Folders group a contract with everything that changed it. Moving a
        document here only changes declared details, never the extracted text.
      </p>
      <div className="modal__field">
        <label>Folder</label>
        <select value={folderSel} onChange={e => setFolderSel(e.target.value)}>
          <option value="">No folder (standalone)</option>
          {folders.filter(f => f.active || f.folder_id === (doc.group ?? "")).map(f => (
            <option key={f.folder_id} value={f.folder_id}>
              {f.name}{f.active ? "" : " (retired)"}
            </option>
          ))}
          {doc.group && !knownFolder && (
            <option value={doc.group}>{doc.group}</option>
          )}
          <option value={NEW_FOLDER}>+ New folder…</option>
        </select>
      </div>
      {folderSel === NEW_FOLDER && (
        <div className="modal__field">
          <label>New folder name</label>
          <input value={newName} placeholder="e.g. Northwind Logistics master agreement"
            onChange={e => setNewName(e.target.value)} autoFocus />
        </div>
      )}
      <div className="modal__field">
        <label>Relationship</label>
        <select value={relation}
          onChange={e => setRelation(e.target.value as NonNullable<UploadIntake["relation"]>)}>
          {RELATIONS.map(r => <option key={r.value} value={r.value}>{r.label}</option>)}
        </select>
      </div>
      {linkRel && (
        <div className="modal__field">
          <label>
            {relation === "novates" ? "Contract it novates"
              : relation === "supersedes" ? "Contract it replaces" : "Contract it amends"}
          </label>
          <select value={parentId} onChange={e => setParentId(e.target.value)}>
            <option value="">Pick the existing contract…</option>
            {parentDocs.map(x => (
              <option key={x.doc_id} value={x.doc_id}>
                {cleanTitle(x.title)}{targetFolder && (x.group ?? "") === targetFolder ? " (same folder)" : ""}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className="modal__grid2">
        <div className="modal__field">
          <label>Document type</label>
          <select value={docType} onChange={e => setDocType(e.target.value)}>
            <option value="">Pick a type…</option>
            {docTypeOptions.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
        <div className="modal__field">
          <label>Document date</label>
          <input type="date" value={docDate} onChange={e => setDocDate(e.target.value)} />
        </div>
      </div>
    </Modal>
  );
}

// ---- Create or rename a folder (the Documents drill-in owns folders now) ----
function FolderEditModal({ folder, notify, onClose, onSaved }: {
  folder: RegistryFolder | null;
  notify: (kind: Toast["kind"], text: string) => void;
  onClose: () => void; onSaved: () => void;
}) {
  const isNew = !folder;
  const [name, setName] = useState(folder?.name ?? "");
  const [saving, setSaving] = useState(false);

  async function save() {
    if (!name.trim() || saving) return;
    setSaving(true);
    try {
      if (isNew) {
        await createRegistryFolder({ name: name.trim() });
      } else {
        await patchRegistryFolder(folder.folder_id, { name: name.trim() });
      }
      notify("ok", `Folder ${isNew ? "created" : "updated"}.`);
      onSaved();
      onClose();
    } catch (e) { notify("err", (e as Error).message); }
    setSaving(false);
  }

  return (
    <Modal
      title={isNew ? "New folder" : `Rename ${folder.name}`}
      onClose={() => { if (!saving) onClose(); }}
      footer={
        <>
          <button className="modal__cancel" disabled={saving} onClick={onClose}>Cancel</button>
          <button className="modal__go" disabled={!name.trim() || saving} onClick={save}>
            {saving ? "Saving…" : "Save"}
          </button>
        </>
      }
    >
      <div className="modal__field">
        <label>Folder name</label>
        <input value={name} placeholder="e.g. Northwind Logistics master agreement"
          onChange={e => setName(e.target.value)} autoFocus />
        {!isNew && (
          <span className="modal__note">
            Renaming only changes the display name. The documents inside stay linked.
          </span>
        )}
      </div>
    </Modal>
  );
}

/** Compact thousands: 1234 → "1.2k", 4200000 → "4.2M". */
const fmtK = (n: number): string =>
  n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M`
    : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);

/** File size for the upload drop zone. */
const fmtMB = (b: number): string =>
  b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`;

const fmtWhen = (iso: string | null): string =>
  iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";

// ---- Shared UI plumbing: modal, toast, pager ---------------------------------

interface Toast { kind: "ok" | "err"; text: string; }

/** A dialog that closes on Escape or on a click outside the box (mousedown, so
 * a drag that starts inside the box doesn't dismiss). */
function Modal({ title, onClose, children, footer }: {
  title: string; onClose: () => void;
  children: React.ReactNode; footer?: React.ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal" onMouseDown={onClose}>
      <div className="modal__box" role="dialog" aria-modal="true" aria-label={title}
        onMouseDown={e => e.stopPropagation()}>
        <div className="modal__head">
          <h3>{title}</h3>
          <button className="modal__x" aria-label="Close" onClick={onClose}>✕</button>
        </div>
        <div className="modal__body">{children}</div>
        {footer && <div className="modal__foot">{footer}</div>}
      </div>
    </div>
  );
}

interface PagerState {
  cur: number; pages: number; total: number; from: number; to: number;
  setPage: (n: number) => void;
}

/** Client-side pagination. Slices AFTER sort + filter so both keep working
 * across the whole set; resetKey snaps back to page 1 when they change. */
function usePager<T>(all: T[], resetKey: string, size = 25): PagerState & { rows: T[] } {
  const [page, setPage] = useState(0);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { setPage(0); }, [resetKey]);
  const pages = Math.max(1, Math.ceil(all.length / size));
  const cur = Math.min(page, pages - 1);
  return {
    rows: all.slice(cur * size, (cur + 1) * size),
    cur, pages, total: all.length,
    from: all.length === 0 ? 0 : cur * size + 1,
    to: Math.min(all.length, (cur + 1) * size),
    setPage,
  };
}

function Pager({ p }: { p: PagerState }) {
  if (p.pages <= 1) return null;
  return (
    <div className="adm__pager">
      <span className="adm__pager-info">{p.from}-{p.to} of {p.total}</span>
      <button disabled={p.cur === 0} onClick={() => p.setPage(p.cur - 1)}>‹ Prev</button>
      <span className="adm__pager-n">{p.cur + 1} / {p.pages}</span>
      <button disabled={p.cur >= p.pages - 1} onClick={() => p.setPage(p.cur + 1)}>Next ›</button>
    </div>
  );
}

// ---- Shared table sort/filter plumbing (every admin table gets both) --------
type SortDir = 1 | -1;
interface Sort { key: string; dir: SortDir; }

/** Click a header once → ascending, again → flips. Empty key = server order. */
function useSort(initKey = "", initDir: SortDir = 1) {
  const [sort, setSort] = useState<Sort>({ key: initKey, dir: initDir });
  const toggle = (k: string) =>
    setSort(p => (p.key === k ? { key: k, dir: (p.dir * -1) as SortDir } : { key: k, dir: 1 }));
  return { sort, toggle };
}

const cmpVals = (a: unknown, b: unknown): number => {
  if (a == null && b == null) return 0;
  if (a == null) return 1;                    // missing values sink to the end
  if (b == null) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b));
};

function sortRows<T>(rows: T[], sort: Sort, get: Record<string, (r: T) => unknown>): T[] {
  const g = get[sort.key];
  if (!g) return rows;
  return [...rows].sort((x, y) => cmpVals(g(x), g(y)) * sort.dir);
}

function SortTh({ k, sort, onSort, children, num }: {
  k: string; sort: Sort; onSort: (k: string) => void;
  children: React.ReactNode; num?: boolean;
}) {
  const on = sort.key === k;
  return (
    <th
      className={`adm__th${num ? " adm__num" : ""}${on ? " is-sorted" : ""}`}
      onClick={() => onSort(k)}
      title="Click to sort"
    >
      {children}{on ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
    </th>
  );
}

// ---- 7-day combo chart: bars = questions per day, line = tokens per day ----

/** Round a data maximum up to a clean axis ceiling (1 / 2 / 2.5 / 5 × 10ⁿ). */
function niceCeil(v: number): number {
  const p = Math.pow(10, Math.floor(Math.log10(Math.max(1, v))));
  const f = v / p;
  const nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return nf * p;
}

/** Axis-label variant of fmtK: 5000 → "5k", not "5.0k". */
const fmtAx = (n: number): string => fmtK(Math.round(n)).replace(/\.0(?=[kM])/, "");

function ComboChart({ series, height = 170 }: {
  series: { day: string; questions: number; tokens: number }[];
  height?: number;
}) {
  // The chart stretches to whatever its container gives it. Rendering at the
  // measured pixel width (instead of scaling a fixed viewBox) keeps text crisp.
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [measured, setMeasured] = useState(0);
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(es => setMeasured(es[0].contentRect.width));
    ro.observe(el);
    setMeasured(el.getBoundingClientRect().width);
    return () => ro.disconnect();
  }, []);
  if (!series.length) return null;

  const width = measured || 560;
  const padL = 46;                            // room for the token scale
  const padR = 30;                            // room for the question scale
  const padT = 8;
  const padB = 18;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;
  const step = plotW / series.length;
  const barW = Math.min(step * 0.52, 44);
  const maxQ = niceCeil(Math.max(1, ...series.map(s => s.questions)));
  const maxT = niceCeil(Math.max(1, ...series.map(s => s.tokens)));
  const xMid = (i: number) => padL + i * step + step / 2;
  const yOf = (frac: number) => padT + plotH - frac * plotH;
  const yTok = (t: number) => yOf(t / maxT);
  const line = series.map((s, i) => `${xMid(i)},${yTok(s.tokens)}`).join(" ");
  // Gridline count picked so token tick values stay round for any nice ceiling.
  const div = maxT / Math.pow(10, Math.floor(Math.log10(maxT))) >= 2.5 ? 5 : 4;
  const ticks = Array.from({ length: div + 1 }, (_, i) => i / div);

  return (
    <div ref={wrapRef} className="adm__chart-wrap">
      <svg
        className="adm__chart" width={width} height={height}
        role="img" aria-label="Daily questions (bars) and tokens (line)"
      >
        {ticks.map(f => {
          const y = yOf(f);
          const qv = maxQ * f;
          return (
            <g key={f}>
              <line
                className={`adm__chart-grid${f === 0 ? " adm__chart-grid--base" : ""}`}
                x1={padL} x2={width - padR} y1={y} y2={y}
              />
              <text className="adm__chart-ax adm__chart-ax--tok" x={padL - 6} y={y + 3} textAnchor="end">
                {fmtAx(maxT * f)}
              </text>
              {Number.isInteger(qv) && (
                <text className="adm__chart-ax adm__chart-ax--q" x={width - padR + 6} y={y + 3} textAnchor="start">
                  {qv}
                </text>
              )}
            </g>
          );
        })}
        {series.map((s, i) => {
          const h = s.questions ? Math.max(2, (s.questions / maxQ) * plotH) : 0;
          return (
            <g key={s.day}>
              {h > 0 && (
                <rect className="adm__chart-bar" x={xMid(i) - barW / 2}
                  y={padT + plotH - h} width={barW} height={h} rx={2}>
                  <title>{`${s.day}: ${s.questions} questions, ${fmtK(s.tokens)} tokens`}</title>
                </rect>
              )}
              <text className="adm__chart-day" x={xMid(i)} y={height - 4} textAnchor="middle">
                {s.day.slice(5)}
              </text>
            </g>
          );
        })}
        <polyline className="adm__chart-line" points={line} fill="none" />
        {series.map((s, i) => (
          <circle key={s.day} className="adm__chart-dot" cx={xMid(i)} cy={yTok(s.tokens)} r={3}>
            <title>{`${s.day}: ${fmtK(s.tokens)} tokens, ${s.questions} questions`}</title>
          </circle>
        ))}
      </svg>
    </div>
  );
}

const USAGE_GET: Record<string, (u: UsageUser) => unknown> = {
  name: u => u.name.toLowerCase(),
  role: u => u.role,
  logins: u => u.logins,
  last_login: u => u.last_login,
  questions: u => u.questions,
  tokens: u => u.prompt_tokens + u.completion_tokens,
  last_active: u => u.last_active,
};

/** Usage metrics: 7-day combo charts (bars = questions, line = tokens) for the
 * whole app and per user, plus the all-time per-account table. Data is owned
 * by the dashboard (the KPI row shares it); Refresh goes through the parent. */
function UsagePanel({ metrics: m, onRefresh }: { metrics: UsageMetrics | null; onRefresh: () => void }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState<string | null>(null);   // email of the charted person
  const { sort, toggle } = useSort("tokens", -1);

  const needle = q.trim().toLowerCase();
  const users = (m?.users ?? [])
    .filter(u => u.questions > 0 || u.logins > 0)
    .filter(u => !needle || (u.name + " " + u.email + " " + u.role).toLowerCase().includes(needle));
  const shown = sortRows(users, sort, USAGE_GET);
  const pager = usePager(shown, `${needle}|${sort.key}|${sort.dir}`);
  const maxTok = Math.max(1, ...users.map(u => u.prompt_tokens + u.completion_tokens));

  // One chart, switched by person. Everyone = the pre-summed daily7 axis.
  // A selected person with no activity this week charts as a flat zero line
  // (honest answer, same axis) rather than an empty box.
  const charts = m?.user_daily ?? [];
  const selUser = sel ? charts.find(u => u.email === sel) ?? null : null;
  const zeroSeries = (m?.daily7 ?? []).map(d => ({ day: d.day, questions: 0, tokens: 0 }));
  const series = sel ? (selUser?.series ?? zeroSeries) : (m?.daily7 ?? []);
  const selName = sel
    ? selUser?.name ?? (m?.users ?? []).find(u => u.email === sel)?.name ?? sel
    : null;
  const pick = (email: string) => setSel(prev => (prev === email ? null : email));

  return (
    <>
      <div className="adm__panel-h">
        <h3>Usage</h3>
        <button className="adm__link" onClick={onRefresh}>Refresh</button>
      </div>
      {!m && <div className="adm__muted" style={{ padding: 14, textAlign: "center" }}>Loading usage…</div>}
      {m && (
        <>
          <div className="adm__use-tots">
            <span><b>{m.totals.questions}</b> questions asked</span>
            <span><b>{fmtK(m.totals.prompt_tokens + m.totals.completion_tokens)}</b> tokens spent</span>
            <span><b>{m.totals.logins}</b> sign-ins</span>
            <span><b>{m.totals.active_users_14d}</b> active users, last 14 days</span>
          </div>

          <div className="adm__use-h">
            Past 7 days · {selName ?? "everyone"}
            {selUser && (
              <span className="adm__use-sel-tot">{selUser.questions} q · {fmtK(selUser.tokens)} tok this week</span>
            )}
            {sel && (
              <button className="adm__link" onClick={() => setSel(null)}>show everyone</button>
            )}
            <span className="adm__chart-legend">
              <i className="adm__chart-legend-bar" /> questions
              <i className="adm__chart-legend-line" /> tokens
            </span>
          </div>
          <ComboChart series={series} />
          {sel && !selUser && (
            <div className="adm__chart-note">{selName} asked no questions in the past 7 days.</div>
          )}

          <div className="adm__use-h">Filter</div>
          <input className="adm__search" placeholder="Filter people…" value={q} onChange={e => setQ(e.target.value)} />
          <div className="adm__scroll">
            {shown.length === 0 ? (
              <div className="adm__muted" style={{ padding: 14, textAlign: "center" }}>
                {needle ? "Nobody matches the filter." : "No usage recorded yet. Sign-ins and questions start counting from now."}
              </div>
            ) : (
              <table className="adm__table">
                <thead>
                  <tr>
                    <SortTh k="name" sort={sort} onSort={toggle}>Person</SortTh>
                    <SortTh k="role" sort={sort} onSort={toggle}>Role</SortTh>
                    <SortTh k="logins" sort={sort} onSort={toggle} num>Sign-ins</SortTh>
                    <SortTh k="last_login" sort={sort} onSort={toggle}>Last sign-in</SortTh>
                    <SortTh k="questions" sort={sort} onSort={toggle} num>Questions</SortTh>
                    <SortTh k="tokens" sort={sort} onSort={toggle}>Tokens</SortTh>
                    <SortTh k="last_active" sort={sort} onSort={toggle}>Last question</SortTh>
                  </tr>
                </thead>
                <tbody>
                  {pager.rows.map(u => {
                    const tok = u.prompt_tokens + u.completion_tokens;
                    return (
                      <tr
                        key={u.email}
                        className={`adm__use-row${sel === u.email ? " is-selected" : ""}`}
                        onClick={() => pick(u.email)}
                        title="Click to chart this person's week"
                      >
                        <td className="adm__acct">
                          <span className="adm__acct-name">{u.name}</span>
                          <span className="adm__acct-meta">{u.email}</span>
                        </td>
                        <td><span className={`adm__pill adm__role--${u.role}`}>{u.role}</span></td>
                        <td className="adm__num">{u.logins || "—"}</td>
                        <td className="adm__fb-when">{fmtWhen(u.last_login)}</td>
                        <td className="adm__num">{u.questions || "—"}</td>
                        <td>
                          <span className="adm__use-tok" title={`${u.prompt_tokens.toLocaleString()} prompt + ${u.completion_tokens.toLocaleString()} completion`}>
                            <span className="adm__use-bar"><i style={{ width: `${(tok / maxTok) * 100}%` }} /></span>
                            {fmtK(tok)}
                          </span>
                        </td>
                        <td className="adm__fb-when">{fmtWhen(u.last_active)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
          <Pager p={pager} />
        </>
      )}
    </>
  );
}

const CATEGORY_LABEL: Record<string, string> = {
  ui_ux: "UI / UX", extraction: "Extraction", answer: "Wrong answer", other: "Other",
};
const FB_STATUSES: FeedbackStatus[] = ["new", "acknowledged", "fixed"];

/** User feedback triage: what demo users reported, newest first. `onCounts`
 * keeps the dashboard KPI + tab badge in sync as items are triaged. */
const FB_GET: Record<string, (it: FeedbackItem) => unknown> = {
  when: it => it.created_at,
  who: it => it.email,
  category: it => it.category,
  message: it => it.message.toLowerCase(),
  status: it => it.status,
};

function FeedbackPanel({ onCounts }: { onCounts?: (c: Record<string, number>) => void }) {
  const [items, setItems] = useState<FeedbackItem[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [filter, setFilter] = useState<FeedbackStatus | null>("new");
  const [expanded, setExpanded] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const { sort, toggle } = useSort("when", -1);

  const load = (f: FeedbackStatus | null) =>
    getFeedback(f)
      .then(d => { setItems(d.items); setCounts(d.counts); onCounts?.(d.counts); setErr(null); })
      .catch(() => setErr("Couldn't load feedback."));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(filter); }, [filter]);

  async function changeStatus(id: number, status: FeedbackStatus) {
    try { await setFeedbackStatus(id, status); load(filter); }
    catch { setErr("Couldn't update status."); }
  }

  const total = (counts.new ?? 0) + (counts.acknowledged ?? 0) + (counts.fixed ?? 0);
  const needle = q.trim().toLowerCase();
  const shown = sortRows(
    items.filter(it => !needle
      || (it.message + " " + it.email + " " + it.category).toLowerCase().includes(needle)),
    sort, FB_GET);
  const pager = usePager(shown, `${filter}|${needle}|${sort.key}|${sort.dir}`);

  return (
    <>
      <div className="adm__panel-h">
        <h3>User feedback <span className="adm__count">{total}</span></h3>
        <button className="adm__link" onClick={() => load(filter)}>Refresh</button>
      </div>
      <div className="adm__fb-chips">
        <button className={`adm__fb-chip${filter === "new" ? " is-on" : ""}`} onClick={() => setFilter("new")}>
          New {counts.new ? <b>{counts.new}</b> : null}
        </button>
        <button className={`adm__fb-chip${filter === "acknowledged" ? " is-on" : ""}`} onClick={() => setFilter("acknowledged")}>
          Acknowledged {counts.acknowledged || ""}
        </button>
        <button className={`adm__fb-chip${filter === "fixed" ? " is-on" : ""}`} onClick={() => setFilter("fixed")}>
          Fixed {counts.fixed || ""}
        </button>
        <button className={`adm__fb-chip${filter === null ? " is-on" : ""}`} onClick={() => setFilter(null)}>All</button>
      </div>
      <input className="adm__search" placeholder="Filter by message, person or category…"
        value={q} onChange={e => setQ(e.target.value)} />
      {err && <div className="adm__err">{err}</div>}
      <div className="adm__scroll">
        {shown.length === 0 ? (
          <div className="adm__muted" style={{ padding: 14, textAlign: "center" }}>
            {q.trim() ? "Nothing matches the filter."
              : filter === "new" ? "No new feedback. Inbox zero." : "Nothing here."}
          </div>
        ) : (
          <table className="adm__table">
            <thead>
              <tr>
                <SortTh k="when" sort={sort} onSort={toggle}>When</SortTh>
                <SortTh k="who" sort={sort} onSort={toggle}>From</SortTh>
                <SortTh k="category" sort={sort} onSort={toggle}>Category</SortTh>
                <SortTh k="message" sort={sort} onSort={toggle}>Message</SortTh>
                <SortTh k="status" sort={sort} onSort={toggle}>Status</SortTh>
              </tr>
            </thead>
            <tbody>
              {pager.rows.map(it => (
                <Fragment key={it.id}>
                  <tr className="adm__fb-row" onClick={() => setExpanded(e => (e === it.id ? null : it.id))}>
                    <td className="adm__fb-when">{new Date(it.created_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</td>
                    <td className="adm__fb-who" title={it.email}>{it.email.split("@")[0]}</td>
                    <td><span className={`adm__pill adm__fb-cat--${it.category}`}>{CATEGORY_LABEL[it.category] ?? it.category}</span></td>
                    <td className="adm__fb-msg">{it.message}</td>
                    <td onClick={e => e.stopPropagation()}>
                      <select className="adm__role" value={it.status}
                        onChange={e => changeStatus(it.id, e.target.value as FeedbackStatus)}>
                        {FB_STATUSES.map(s => <option key={s} value={s}>{s}</option>)}
                      </select>
                    </td>
                  </tr>
                  {expanded === it.id && (
                    <tr className="adm__edit-row">
                      <td colSpan={5}>
                        <div className="adm__fb-detail">
                          <div className="adm__fb-full">{it.message}</div>
                          {it.context && (
                            <dl className="adm__fb-ctx">
                              {["tab", "thread_title", "doc_id", "last_question"].map(k => {
                                const v = (it.context as Record<string, unknown>)[k];
                                return v ? <div key={k}><dt>{k.replace("_", " ")}</dt><dd>{String(v)}</dd></div> : null;
                              })}
                              {(it.context as Record<string, unknown>).reported_answer ? (
                                <div><dt>reported answer</dt><dd>{String((it.context as Record<string, unknown>).reported_answer)}</dd></div>
                              ) : (it.context as Record<string, unknown>).last_answer_head ? (
                                <div><dt>last answer</dt><dd>{String((it.context as Record<string, unknown>).last_answer_head)}</dd></div>
                              ) : null}
                            </dl>
                          )}
                          {(it.attachments?.length ?? 0) > 0 && (
                            <div className="adm__fb-shots">
                              {it.attachments!.map(name => (
                                <FeedbackShot key={name} name={name} />
                              ))}
                            </div>
                          )}
                          {it.status_by && (
                            <div className="adm__muted" style={{ fontSize: 11 }}>
                              {it.status} by {it.status_by}
                            </div>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <Pager p={pager} />
    </>
  );
}

/** One feedback screenshot: thumbnail, click to open a full-screen popup.
 * Fetched with auth headers (the attachment route is admin-gated). */
function FeedbackShot({ name }: { name: string }) {
  const [big, setBig] = useState(false);
  useEffect(() => {
    if (!big) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setBig(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [big]);
  return (
    <>
      <button
        type="button"
        className="adm__fb-shot"
        title="Click to enlarge"
        onClick={() => setBig(true)}
      >
        <AuthedImage src={feedbackAttachmentUrl(name)} alt="screenshot from the reporter" />
      </button>
      {big && (
        <div
          className="adm__fb-lightbox"
          role="dialog"
          aria-label="Screenshot"
          onClick={() => setBig(false)}
        >
          <AuthedImage src={feedbackAttachmentUrl(name)} alt="screenshot from the reporter, full size" />
          <button className="adm__fb-lightbox-close" aria-label="Close" onClick={() => setBig(false)}>×</button>
        </div>
      )}
    </>
  );
}

const KIND_LABEL: Record<string, string> = {
  verification: "Verification", ontology: "Ontology", account: "Accounts",
  document: "Documents", feedback: "Feedback", other: "Other",
};
const KINDS: ActivityKind[] = ["verification", "ontology", "account", "document", "feedback"];

const ACT_GET: Record<string, (it: ActivityItem) => unknown> = {
  when: it => it.at,
  who: it => it.actor_name.toLowerCase(),
  kind: it => it.kind,
};

/** The changelog: who did what, when — verification votes, ontology edits,
 * account changes, document uploads/extractions, feedback. Newest first. */
function ActivityPanel() {
  const [items, setItems] = useState<ActivityItem[] | null>(null);
  const [kind, setKind] = useState<ActivityKind | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const { sort, toggle } = useSort("when", -1);

  const load = () =>
    getActivity()
      .then(d => { setItems(d); setErr(null); })
      .catch(() => setErr("Couldn't load the activity log."));
  useEffect(() => { load(); }, []);

  const needle = q.trim().toLowerCase();
  const visible = sortRows(
    (items ?? [])
      .filter(it => !kind || it.kind === kind)
      .filter(it => !needle
        || (it.actor_name + " " + it.action + " " + it.target + " " + it.detail).toLowerCase().includes(needle)),
    sort, ACT_GET);
  const pager = usePager(visible, `${kind}|${needle}|${sort.key}|${sort.dir}`);

  return (
    <>
      <div className="adm__panel-h">
        <h3>Activity <span className="adm__count">{items?.length ?? 0}</span></h3>
        <button className="adm__link" onClick={load}>Refresh</button>
      </div>
      <div className="adm__fb-chips">
        <button className={`adm__fb-chip${kind === null ? " is-on" : ""}`} onClick={() => setKind(null)}>All</button>
        {KINDS.map(k => (
          <button key={k} className={`adm__fb-chip${kind === k ? " is-on" : ""}`} onClick={() => setKind(k)}>
            {KIND_LABEL[k]}
          </button>
        ))}
      </div>
      <input className="adm__search" placeholder="Filter by person, action or target…"
        value={q} onChange={e => setQ(e.target.value)} />
      {err && <div className="adm__err">{err}</div>}
      <div className="adm__scroll">
        {items === null ? (
          <div className="adm__muted" style={{ padding: 14, textAlign: "center" }}>Loading…</div>
        ) : visible.length === 0 ? (
          <div className="adm__muted" style={{ padding: 14, textAlign: "center" }}>
            {needle || kind ? "Nothing matches the filter." : "No activity yet."}
          </div>
        ) : (
          <table className="adm__table">
            <thead>
              <tr>
                <SortTh k="when" sort={sort} onSort={toggle}>When</SortTh>
                <SortTh k="who" sort={sort} onSort={toggle}>Who</SortTh>
                <SortTh k="kind" sort={sort} onSort={toggle}>Kind</SortTh>
                <th>What</th>
              </tr>
            </thead>
            <tbody>
              {pager.rows.map((it, i) => (
                <tr key={`${pager.cur}-${i}`} className="adm__act-row">
                  <td className="adm__fb-when">
                    {new Date(it.at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
                  </td>
                  <td className="adm__fb-who" title={it.actor}>{humanActor(it.actor_name)}</td>
                  <td><span className={`adm__pill adm__act--${it.kind}`}>{KIND_LABEL[it.kind] ?? it.kind}</span></td>
                  <td className="adm__act-what">
                    {it.action}
                    {it.target && <span className="adm__act-target"> · {it.target}</span>}
                    {it.detail && <span className="adm__act-detail" title={it.detail}>: {it.detail}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <Pager p={pager} />
    </>
  );
}

function Stat({ label, value, hint, accent }: { label: string; value: React.ReactNode; hint?: string; accent?: boolean }) {
  return (
    <div className={`adm__stat${accent ? " adm__stat--accent" : ""}`}>
      <div className="adm__stat-v">{value}</div>
      <div className="adm__stat-l">{label}</div>
      {hint && <div className="adm__stat-h">{hint}</div>}
    </div>
  );
}

// Highest access first, then A→Z — so the list always reads admins → confidential
// → everyone else, and stays put when roles change or accounts are added.
const ROLE_ORDER: Record<string, number> = { admin: 0, confidential: 1, default: 2 };

const ACCT_GET: Record<string, (a: Account) => unknown> = {
  name: a => (a.name || a.email).toLowerCase(),
  role: a => ROLE_ORDER[a.role] ?? 9,
  verifier: a => (a.role === "admin" || a.verifier ? 0 : 1),
};

function AccountManager({ accounts, onChanged }: { accounts: Account[]; onChanged: () => void }) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [newName, setNewName] = useState("");
  const [newRole, setNewRole] = useState("default");
  const [error, setError] = useState<string | null>(null);
  // Inline editor: click an account → edit its name / email / title in place.
  const [editing, setEditing] = useState<string | null>(null);
  const [eName, setEName] = useState("");
  const [eEmail, setEEmail] = useState("");
  const [eTitle, setETitle] = useState("");
  const { sort, toggle } = useSort();

  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase();
    const base = s ? accounts.filter(a => (a.name + a.email + a.title).toLowerCase().includes(s)) : accounts;
    // Default order (no header clicked): highest access first, then A→Z.
    const dflt = [...base].sort((a, b) =>
      (ROLE_ORDER[a.role] ?? 9) - (ROLE_ORDER[b.role] ?? 9)
      || (a.name || a.email).localeCompare(b.name || b.email));
    return sortRows(dflt, sort, ACCT_GET);
  }, [accounts, q, sort]);
  const pager = usePager(filtered, `${q.trim().toLowerCase()}|${sort.key}|${sort.dir}`);

  async function changeRole(email: string, role: string) {
    setBusy(email); setError(null);
    try { await setAccountRole(email, role); onChanged(); }
    catch { setError("Couldn't change role."); }
    finally { setBusy(null); }
  }

  async function changeVerifier(email: string, verifier: boolean) {
    setBusy(email); setError(null);
    try { await editAccount(email, { verifier }); onChanged(); }
    catch { setError("Couldn't change the verifier flag."); }
    finally { setBusy(null); }
  }

  function openEdit(a: Account) {
    setEditing(a.email);
    setEName(a.name || ""); setEEmail(a.email); setETitle(a.title || "");
    setError(null);
  }

  async function saveEdit() {
    if (!editing) return;
    setBusy(editing); setError(null);
    try {
      const newAddr = eEmail.trim().toLowerCase();
      await editAccount(editing, {
        name: eName.trim(),
        title: eTitle.trim(),
        ...(newAddr && newAddr !== editing ? { new_email: newAddr } : {}),
      });
      setEditing(null);
      onChanged();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(null); }
  }

  async function submitNew() {
    setBusy("new"); setError(null);
    try {
      await addAccount({ email: newEmail.trim(), name: newName.trim(), role: newRole });
      setNewEmail(""); setNewName(""); setNewRole("default"); setAdding(false); onChanged();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(null); }
  }

  return (
    <>
      <div className="adm__panel-h">
        <h3>Accounts <span className="adm__count">{accounts.length}</span></h3>
        <button className="adm__link" onClick={() => setAdding(a => !a)}>{adding ? "Cancel" : "+ Add account"}</button>
      </div>

      {adding && (
        <div className="adm__addform">
          <input placeholder="name@example.com" value={newEmail} onChange={e => setNewEmail(e.target.value)} />
          <input placeholder="Name" value={newName} onChange={e => setNewName(e.target.value)} />
          <select value={newRole} onChange={e => setNewRole(e.target.value)}>
            {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
          </select>
          <button disabled={busy === "new" || !newEmail.includes("@")} onClick={submitNew}>Add</button>
        </div>
      )}
      {error && <div className="adm__err">{error}</div>}

      <input className="adm__search" placeholder="Search accounts…" value={q} onChange={e => setQ(e.target.value)} />
      <div className="adm__scroll">
        <table className="adm__table">
          <thead>
            <tr>
              <SortTh k="name" sort={sort} onSort={toggle}>Person</SortTh>
              <SortTh k="role" sort={sort} onSort={toggle}>Access level</SortTh>
              <SortTh k="verifier" sort={sort} onSort={toggle}>Verifier</SortTh>
            </tr>
          </thead>
          <tbody>
            {pager.rows.map(a => (
              <Fragment key={a.email}>
                <tr
                  className={`adm__acct-row${editing === a.email ? " is-editing" : ""}`}
                  onClick={() => (editing === a.email ? setEditing(null) : openEdit(a))}
                  title="Click to edit this account"
                >
                  <td className="adm__acct">
                    <span className="adm__acct-name">{a.name || a.email}</span>
                    <span className="adm__acct-meta">{a.title || a.email}</span>
                  </td>
                  <td onClick={e => e.stopPropagation()}>
                    <select className={`adm__role adm__role--${a.role}`} value={a.role} disabled={busy === a.email}
                      onChange={e => changeRole(a.email, e.target.value)}>
                      {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
                    </select>
                  </td>
                  <td
                    className="adm__verify"
                    onClick={e => e.stopPropagation()}
                    title={a.role === "admin"
                      ? "Admins can always verify"
                      : "Approved verifier: may vote on field verifications in the Review tab"}
                  >
                    <label className="adm__verify-l">
                      <input
                        type="checkbox"
                        checked={a.role === "admin" || !!a.verifier}
                        disabled={a.role === "admin" || busy === a.email}
                        onChange={e => changeVerifier(a.email, e.target.checked)}
                      />
                      verifier
                    </label>
                  </td>
                </tr>
                {editing === a.email && (
                  <tr className="adm__edit-row">
                    <td colSpan={3}>
                      <div className="adm__addform" onClick={e => e.stopPropagation()}>
                        <input placeholder="Name" value={eName} onChange={e => setEName(e.target.value)} />
                        <input placeholder="name@example.com" value={eEmail} onChange={e => setEEmail(e.target.value)} />
                        <input placeholder="Job title" value={eTitle} onChange={e => setETitle(e.target.value)} />
                        <button disabled={busy === a.email || !eEmail.includes("@")} onClick={saveEdit}>Save</button>
                        <button className="adm__cancel" onClick={() => setEditing(null)}>Cancel</button>
                      </div>
                      {eEmail.trim().toLowerCase() !== a.email && (
                        <div className="adm__edit-note">
                          Changing the email moves this person's login and chat history to the new address.
                        </div>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      <Pager p={pager} />
    </>
  );
}
