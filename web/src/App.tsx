import { Suspense, lazy, useCallback, useEffect, useMemo, useState } from "react";
import { getEvidence, getSetupStatus, listDocuments } from "./api";
import { useBranding, useDocNoun } from "./branding";
import { sortDocs } from "./docmeta";
import { Citation, DocumentMeta } from "./types";
import { ChatPanel } from "./components/ChatPanel";
import { Sidebar } from "./components/Sidebar";
import { JobTicker } from "./components/JobTicker";
import { AdminJobsProvider } from "./hooks/useAdminJobs";
import { FeedbackButton } from "./components/FeedbackButton";
import { HelpButton } from "./components/HelpButton";
import { IconDocument } from "./components/Icon";
import { LoginScreen } from "./components/LoginScreen";
import { SetupWizard } from "./components/SetupWizard";
import { WarmupBanner } from "./components/WarmupBanner";
import { useAuth } from "./auth";
import { Thread, useThreads } from "./hooks/useThreads";
import { saveUi } from "./uiState";

// Heavy views load on demand (code splitting): login + chat ship in the first
// bundle, the PDF engine / graph layout / dashboards arrive when their tab or
// a citation first needs them. Cuts the first paint, which compounds with the
// sleeping host's cold start.
const PdfPanel = lazy(() => import("./components/PdfPanel").then(m => ({ default: m.PdfPanel })));
const GraphExplorer = lazy(() => import("./components/GraphExplorer").then(m => ({ default: m.GraphExplorer })));
const OntologyView = lazy(() => import("./components/OntologyView").then(m => ({ default: m.OntologyView })));
const ReviewView = lazy(() => import("./components/ReviewView").then(m => ({ default: m.ReviewView })));
const KnowledgeView = lazy(() => import("./components/KnowledgeView").then(m => ({ default: m.KnowledgeView })));
const AdminDashboard = lazy(() => import("./components/AdminDashboard").then(m => ({ default: m.AdminDashboard })));

const lazyFallback = <div className="lazy-fallback">Loading…</div>;

type Tab = "chat" | "explore" | "review" | "knowledge" | "admin" | "ontology";

// Auth gate: no session → login; a session → the app. Kept as a thin wrapper so the main
// app's hooks always run in the same order (no conditional-hook rule violation).
export default function App() {
  const { user, loading } = useAuth();
  // Setup gate: an instance that has never been configured shows the wizard
  // instead of anything else, no session or documents to speak of yet.
  // Checked once per load; the wizard flips this itself when it finishes (it
  // also signs the new admin in directly, so the next render usually goes
  // straight to MainApp rather than back through the login screen). Fails
  // OPEN on a network hiccup — a transient /setup/status failure must not
  // permanently trap an already-configured instance behind a stuck wizard.
  const [setupComplete, setSetupComplete] = useState<boolean | null>(null);
  useEffect(() => {
    getSetupStatus().then(s => setSetupComplete(s.complete)).catch(() => setSetupComplete(true));
  }, []);
  // The warmup banner sits OUTSIDE the auth gate: a cold start hits the very
  // first /auth/me and login calls, exactly when the rest of the UI is blank.
  const body =
    setupComplete === null || loading ? <div className="app-boot">Loading…</div>
    : setupComplete === false ? <SetupWizard onDone={() => setSetupComplete(true)} />
    : !user ? <LoginScreen /> : <MainApp />;
  return (
    <>
      <WarmupBanner />
      {body}
    </>
  );
}

function MainApp() {
  const { appName } = useBranding();
  const [docs, setDocs] = useState<DocumentMeta[]>([]);
  // Chat scope: a SET of contracts the agent may search. Empty = all
  // documents (cross-doc). The backend takes doc_ids as a list and every
  // retrieval tool filters `doc_id IN $doc_ids`, so selecting 4 contracts
  // searches exactly those 4.
  const [selectedDocIds, setSelectedDocIds] = useState<string[]>([]);
  // PDF viewer doc — decoupled from chat scope so clicking a citation in a
  // cross-doc answer opens the cited contract WITHOUT re-scoping chat.
  const [viewerDocId, setViewerDocId] = useState<string | null>(null);
  // Straight from finishing the setup wizard onto the tab where uploading
  // happens — set once by SetupWizard right before its post-finish reload,
  // consumed (and cleared) on the very next mount of the real app.
  const [tab, setTab] = useState<Tab>(() => {
    const land = sessionStorage.getItem("verbatim.postSetupLandTab");
    if (land) sessionStorage.removeItem("verbatim.postSetupLandTab");
    return land === "admin" ? "admin" : "chat";
  });
  const [focusedEvidence, setFocusedEvidence] = useState<{
    citation: Citation;
    cluster: Citation[];
  } | null>(null);
  // PDF panel is toggleable so the user can give the chat full width.
  // Clicking a citation auto-opens it (no point hiding the source the
  // user just asked to inspect).
  const [pdfVisible, setPdfVisible] = useState<boolean>(true);
  // Mobile-only: the sidebar collapses into a drawer behind a ☰ button.
  const [navOpen, setNavOpen] = useState(false);
  // RBAC comes from the logged-in ACCOUNT, enforced SERVER-SIDE from the
  // session token — the frontend no longer sends a role anywhere. `restricted`
  // only drives blur styling for Default users.
  const { user, tabs, logout } = useAuth();
  const restricted = user?.role === "default";
  // Version counter an open PDF watches to re-fetch its blur regions. The
  // legacy label-policy that drives the blur is config-static now (the old
  // runtime policy panel is gone), so this stays constant.
  const [policyVersion] = useState<number>(0);

  const threads = useThreads(user?.email ?? null);

  // If the current tab isn't allowed for this account (role changed, deep
  // state from an older session), snap back to chat.
  useEffect(() => {
    if (!tabs.includes(tab)) setTab("chat");
  }, [tabs, tab]);

  useEffect(() => {
    // No auto-scope: the default is ALL documents. The PDF pane stays on a
    // purposeful empty state until a citation or an explicit doc click picks
    // one — an arbitrary cover page reads as noise, not orientation.
    // Refetches on every tab switch, not just on mount: uploading in Admin
    // then switching to Chat used to show a stale, empty doc list until a
    // hard reload, and any citation click silently failed to open its
    // source for the same reason (viewerDoc couldn't resolve the new doc_id).
    // `cancelled` drops a response from a tab switch the user has already
    // clicked past, so a slow fetch can't overwrite a newer, faster one.
    let cancelled = false;
    listDocuments()
      .then(d => { if (!cancelled) setDocs(sortDocs(d)); })
      .catch(err => console.error("listDocuments:", err));
    return () => { cancelled = true; };
  }, [tab]);

  const selectedDocs = useMemo(
    () => docs.filter(d => selectedDocIds.includes(d.doc_id)),
    [docs, selectedDocIds],
  );

  const viewerDoc = useMemo(
    () => docs.find(d => d.doc_id === viewerDocId) ?? null,
    [docs, viewerDocId],
  );

  // Explore graph scopes to a single contract only when exactly one is
  // selected; otherwise it spans every doc (the cross-doc view is the point).
  const explorerDoc = selectedDocs.length === 1 ? selectedDocs[0] : null;

  // Toggle a contract in/out of the search scope.
  const toggleDocScope = useCallback((id: string) => {
    setSelectedDocIds(prev =>
      prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id],
    );
    setFocusedEvidence(null);
  }, []);

  const clearScope = useCallback(() => {
    setSelectedDocIds([]);
    setFocusedEvidence(null);
  }, []);

  // Open a contract in the PDF viewer without touching the search scope.
  const openInViewer = useCallback((id: string) => {
    setViewerDocId(id);
    setPdfVisible(true);
  }, []);

  const handleSelectThread = (id: string) => {
    threads.setActive(id);
    setTab("chat");
    setFocusedEvidence(null);
  };

  // Explore-graph "View in PDF": fetch the evidence atom, build a Citation
  // from it (same shape the chat flow uses), and open the PDF highlight.
  // Reuses the existing /evidence + PdfPanel bbox machinery — no new path.
  const handleViewEvidence = useCallback(
    async (evidenceId: string, docId: string) => {
      try {
        const ev = await getEvidence(evidenceId);
        const citation: Citation = {
          evidence_id: ev.evidence_id,
          doc_id: ev.doc_id,
          page_no: ev.page_no,
          rects: ev.rects ?? null,
          section_id: ev.section_id,
          section_num: ev.section_num,
          section_title: ev.section_title,
          snippet: ev.snippet,
          bbox: ev.bbox,
          fact_label: ev.fact_label,
          fact_id: null,
          fact_summary: null,
          score: 0,
        };
        setFocusedEvidence({ citation, cluster: [citation] });
        setViewerDocId(docId);
        setPdfVisible(true);
      } catch (err) {
        console.error("viewEvidence:", err);
      }
    },
    [],
  );

  const handleNewChat = () => {
    threads.startNew();
    setTab("chat");
    setFocusedEvidence(null);
  };

  // The one review jump: with a doc id the Review tab opens straight on that
  // document, without one it opens on the progress overview. ReviewView reads
  // these hints when it mounts. Shared by AdminDashboard and the job ticker.
  const goReview = useCallback((docId?: string) => {
    if (docId) {
      saveUi("review.mode", "doc");
      saveUi("review.docId", docId);
    } else {
      saveUi("review.mode", "overview");
    }
    setTab("review");
  }, []);

  // Job-ticker click: land on Admin > Documents. The saved hints cover a
  // fresh mount; the event covers an already-open Admin tab.
  const goAdminDocs = useCallback(() => {
    saveUi("admin.section", "docs");
    saveUi("admin.folder", null);
    window.dispatchEvent(new Event("admin:goto-docs"));
    setTab("admin");
  }, []);

  const recents: Thread[] = threads.list;
  const activeThread = threads.active;

  return (
    <AdminJobsProvider admin={user?.role === "admin"}>
    <div className="mobilebar">
      <button
        type="button"
        onClick={() => setNavOpen(o => !o)}
        aria-label="Menu"
        aria-expanded={navOpen}
      >☰</button>
      <span>{appName}</span>
    </div>
    {navOpen && <div className="mobile-scrim" onClick={() => setNavOpen(false)} />}
    <div className={`app${navOpen ? " app--nav-open" : ""}`}>
      <div
        style={{ display: "contents" }}
        // Any tap on a sidebar control also closes the mobile drawer — the
        // user picked something, show them the result. No-op on desktop.
        onClickCapture={e => {
          if (navOpen && (e.target as HTMLElement).closest("button")) setNavOpen(false);
        }}
      >
      <Sidebar
        tab={tab}
        onTab={setTab}
        docs={docs}
        selectedDocIds={selectedDocIds}
        viewerDocId={viewerDocId}
        onToggleDocScope={toggleDocScope}
        onClearScope={clearScope}
        onOpenDoc={openInViewer}
        threads={recents.map(t => ({ id: t.id, title: t.title }))}
        activeThreadId={activeThread?.id ?? null}
        onSelectThread={handleSelectThread}
        onDeleteThread={threads.remove}
        onClearConversations={() => {
          threads.clearAll();
          setTab("chat");
          setFocusedEvidence(null);
        }}
        onNewChat={handleNewChat}
        user={user}
        onLogout={logout}
        tabs={tabs}
      />
      </div>

      <div className="app-main">
      {user?.role === "admin" && (
        <JobTicker docs={docs} onOpenAdminDocs={goAdminDocs} onReviewDoc={goReview} />
      )}
      {tab === "chat" ? (
        <div className={`content ${pdfVisible ? "content--split" : "content--solo"}`}>
          <ChatPanel
            docIds={selectedDocIds}
            thread={activeThread}
            onUpdateThread={threads.update}
            pdfVisible={pdfVisible}
            onShowPdf={() => setPdfVisible(true)}
            restricted={restricted}
            onOpenDoc={openInViewer}
            onFocusEvidence={(citation, cluster) => {
              setFocusedEvidence({ citation, cluster });
              // Citation click → make sure the source is on screen, and
              // showing the document the citation actually came from
              // (cross-doc answers cite multiple contracts).
              setViewerDocId(citation.doc_id);
              setPdfVisible(true);
            }}
          />
          {pdfVisible && (
            viewerDoc ? (
              <Suspense fallback={<div className="pane pane--source">{lazyFallback}</div>}>
                <PdfPanel
                  doc={viewerDoc}
                  focused={focusedEvidence?.citation ?? null}
                  cluster={focusedEvidence?.cluster ?? []}
                  restricted={restricted}
                  policyVersion={policyVersion}
                  onFocusCitation={c => setFocusedEvidence(prev =>
                    prev ? { ...prev, citation: c } : { citation: c, cluster: [c] })}
                  onClose={() => setPdfVisible(false)}
                />
              </Suspense>
            ) : (
              <SourcePlaceholder docs={docs} onClose={() => setPdfVisible(false)} />
            )
          )}
        </div>
      ) : tab === "explore" ? (
        <div className={`content ${pdfVisible && focusedEvidence ? "content--split" : ""}`}>
          <Suspense fallback={lazyFallback}>
            <GraphExplorer doc={explorerDoc} onViewEvidence={handleViewEvidence} />
            {pdfVisible && focusedEvidence && (
              <PdfPanel
                doc={viewerDoc}
                focused={focusedEvidence.citation}
                cluster={focusedEvidence.cluster}
                restricted={restricted}
                policyVersion={policyVersion}
                onFocusCitation={c => setFocusedEvidence(prev =>
                  prev ? { ...prev, citation: c } : { citation: c, cluster: [c] })}
                onClose={() => { setPdfVisible(false); setFocusedEvidence(null); }}
              />
            )}
          </Suspense>
        </div>
      ) : tab === "review" ? (
        <div className="content"><Suspense fallback={lazyFallback}><ReviewView /></Suspense></div>
      ) : tab === "knowledge" ? (
        <div className="content"><Suspense fallback={lazyFallback}><KnowledgeView /></Suspense></div>
      ) : tab === "ontology" ? (
        <div className="content"><Suspense fallback={lazyFallback}><OntologyView /></Suspense></div>
      ) : (
        <div className="content">
          <Suspense fallback={lazyFallback}>
            <AdminDashboard
              onOpenOntology={() => setTab("ontology")}
              onGoReview={goReview}
            />
          </Suspense>
        </div>
      )}
      </div>

      <HelpButton />
      <FeedbackButton
        tab={tab}
        thread={activeThread}
        viewerDocId={viewerDocId}
        selectedDocIds={selectedDocIds}
      />
    </div>
    </AdminJobsProvider>
  );
}

/** The source pane before anything is cited: a purposeful empty state instead
 * of an arbitrary contract cover page. */
function SourcePlaceholder({ docs, onClose }: { docs: DocumentMeta[]; onClose: () => void }) {
  const noun = useDocNoun();
  const nFamilies = new Set(docs.map(d => d.group ?? d.doc_id)).size;
  return (
    <div className="pane pane--source srcempty">
      <div className="pane__header">
        <IconDocument />
        <div className="pane__title">Source</div>
        <div className="pane__header-spacer" />
        <button
          type="button" className="pane__icon-btn" onClick={onClose}
          aria-label="Hide source" title="Hide source"
        >×</button>
      </div>
      <div className="srcempty__body">
        <div className="srcempty__mark" aria-hidden><IconDocument /></div>
        <div className="srcempty__title">Sources appear here</div>
        <p>
          Ask a question, then click a citation chip in the answer to see the
          exact clause highlighted on the page it came from.
        </p>
        {docs.length > 0 && (
          <div className="srcempty__stats">
            <b>{docs.length}</b> document{docs.length === 1 ? "" : "s"} in{" "}
            <b>{nFamilies}</b> famil{nFamilies === 1 ? "y" : "ies"} loaded
          </div>
        )}
        <p className="srcempty__hint">
          You can also open any {noun.one} directly from the picker in
          the sidebar.
        </p>
      </div>
    </div>
  );
}
