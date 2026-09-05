import {
  ChatMessage,
  Citation,
  CitationRect,
  DocumentMeta,
  EvidenceDetail,
  GraphPayload,
  IntentPlan,
  PolicyState,
} from "./types";

// FastAPI URL. Set VITE_API_BASE at build time to point at a different
// backend. Defaults: localhost:8000 in dev; SAME ORIGIN in production
// builds (nginx serves the static bundle and reverse-proxies the API,
// so no CORS and no hardcoded host).
const _env = (import.meta as unknown as {
  env: { VITE_API_BASE?: string; VITE_API_KEY?: string; DEV?: boolean };
}).env;
const API_BASE = _env.VITE_API_BASE || (_env.DEV ? "http://127.0.0.1:8000" : "");
const API_KEY = _env.VITE_API_KEY || "";

export const apiUrl = (path: string): string => `${API_BASE}${path}`;

// ---- Cold-start handling ----------------------------------------------------
// The hosted demo scales to zero: the api and the graph database sleep when
// idle, and requests in the ~30-60s wake window fail with 502/503/504 (nginx
// rewrites its own gateway errors into the same JSON "warming" signal the api
// sends while Neo4j boots). apiFetch() recognises those responses, broadcasts
// an "api:warming" event the UI shows as a banner, and transparently retries
// safe (GET) requests until the stack is up. Mutations are never auto-retried
// (they may have partially applied server-side); they still flip the banner so
// the user knows why a click failed. The banner clears on the first response
// that gets through.
const WARMING_STATUS = new Set([502, 503, 504]);
// ~50s of patience across retries, matching the observed cold boot.
const WARMING_RETRY_MS = [1500, 2000, 3000, 4000, 5000, 6000, 8000, 8000, 8000, 8000];

// A sleeping host often HOLDS the first request open while the container boots
// instead of failing fast — nothing errors, nothing retries, the page just
// freezes. Detect that: when the tab has been quiet long enough for the host
// to have fallen asleep and the next request gets no answer within a couple of
// seconds, treat it as a wake in progress and show the banner. The idle guard
// keeps ordinary slow calls (big documents, long answers) from tripping it.
const HOLD_NOTICE_MS = 2500;
const IDLE_BEFORE_HOLD_MS = 60_000;
let _lastSettledAt = 0; // 0 = no response ever → a fresh load counts as idle

let _warming = false;
let _warmingSince: number | null = null;
const _setWarming = (w: boolean): void => {
  if (w === _warming) return;
  _warming = w;
  _warmingSince = w ? Date.now() : null;
  try { window.dispatchEvent(new CustomEvent("api:warming", { detail: w })); } catch { /* non-browser env */ }
};
export const isApiWarming = (): boolean => _warming;
/** Epoch ms when the current warming spell began (null when not warming) —
 * lets the banner show honest elapsed time across mounts. */
export const apiWarmingSince = (): number | null => _warmingSince;

const _sleep = (ms: number) => new Promise<void>(res => setTimeout(res, ms));

/** fetch() with cold-start awareness. Auto-retries GETs (and calls that opt in
 * via `retry`) on gateway/warming responses; every method flips the banner. */
export async function apiFetch(
  url: string, init?: RequestInit, opts?: { retry?: boolean },
): Promise<Response> {
  const method = (init?.method ?? "GET").toUpperCase();
  const canRetry = opts?.retry ?? method === "GET";
  for (let attempt = 0; ; attempt++) {
    // Silent-hold detector: armed only after an idle gap (or on a fresh load),
    // so a request the waking host holds open still surfaces the banner.
    const idle = Date.now() - _lastSettledAt > IDLE_BEFORE_HOLD_MS;
    const holdTimer = idle
      ? setTimeout(() => {
          // Another request may have answered while this one waited — then the
          // server is up and this is just a slow call, not a wake.
          if (Date.now() - _lastSettledAt > HOLD_NOTICE_MS) _setWarming(true);
        }, HOLD_NOTICE_MS)
      : null;
    try {
      const resp = await fetch(url, init);
      _lastSettledAt = Date.now();
      if (!WARMING_STATUS.has(resp.status)) {
        _setWarming(false);
        return resp;
      }
      _setWarming(true);
      if (!canRetry || attempt >= WARMING_RETRY_MS.length) return resp;
    } catch (err) {
      // Network-level failure (server unreachable). Retry safe calls, but do
      // not claim "warming" — the user may simply be offline.
      if ((err as Error).name === "AbortError" || !canRetry
          || attempt >= WARMING_RETRY_MS.length) throw err;
    } finally {
      if (holdTimer) clearTimeout(holdTimer);
    }
    await _sleep(WARMING_RETRY_MS[Math.min(attempt, WARMING_RETRY_MS.length - 1)]);
  }
}

// Passwordless session token (from /auth/login). Stored in localStorage so the plain
// authHeaders() function — used everywhere, including the SSE chat stream — can attach it
// without threading React state through every call site.
const SESSION_KEY = "verbatim.session";
const LEGACY_SESSION_KEY = "engineering_agent.session";
export const getSessionToken = (): string => {
  try {
    const tok = localStorage.getItem(SESSION_KEY);
    if (tok) return tok;
    // One-time migration off the old key so an existing tab stays signed in.
    const old = localStorage.getItem(LEGACY_SESSION_KEY);
    if (old) {
      localStorage.setItem(SESSION_KEY, old);
      localStorage.removeItem(LEGACY_SESSION_KEY);
      return old;
    }
    return "";
  } catch { return ""; }
};
export const setSessionToken = (t: string | null): void => {
  try { t ? localStorage.setItem(SESSION_KEY, t) : localStorage.removeItem(SESSION_KEY); } catch { /* no storage */ }
};

/** Auth headers for every API request: optional API key + the user session token. */
export const authHeaders = (): Record<string, string> => {
  const h: Record<string, string> = {};
  if (API_KEY) h["X-API-Key"] = API_KEY;
  const tok = getSessionToken();
  if (tok) h["X-User-Token"] = tok;
  return h;
};

/** A 401 means the session died (account deleted / token cleared server-side).
 * Broadcast it so the auth provider drops the user back to the login screen
 * instead of every view failing quietly. */
export const notifyAuthExpired = (): void => {
  setSessionToken(null);
  try { window.dispatchEvent(new Event("auth:expired")); } catch { /* SSR/test */ }
};

const guard401 = (r: Response): Response => {
  if (r.status === 401) notifyAuthExpired();
  return r;
};

// ---- Auth (passwordless email login against the accounts table) ----
export interface Account { email: string; name: string; title: string; role: string; zone: string; verifier?: boolean; }
export async function getAccounts(): Promise<Account[]> {
  const r = await apiFetch(apiUrl("/auth/accounts"), { headers: authHeaders() });
  if (!r.ok) throw new Error(`/auth/accounts: ${r.status}`);
  const { accounts } = await r.json();
  return accounts;
}
export interface Capabilities { tabs: string[]; }
export async function apiLogin(email: string): Promise<{ token: string; user: Account; capabilities?: Capabilities }> {
  // retry: creating a session is safe to repeat, so a login clicked while the
  // server is still waking simply lands once it is up (no error flash).
  const r = await apiFetch(apiUrl("/auth/login"), {
    method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ email }),
  }, { retry: true });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(String((data as { detail?: string }).detail || `login: ${r.status}`));
  return data;
}
export async function apiMe(): Promise<{ user: Account; capabilities?: Capabilities } | null> {
  const r = await apiFetch(apiUrl("/auth/me"), { headers: authHeaders() });
  if (!r.ok) return null;
  return r.json();
}
export async function apiLogout(): Promise<void> {
  try { await apiFetch(apiUrl("/auth/logout"), { method: "POST", headers: authHeaders() }); } catch { /* ignore */ }
}

// ---- Admin (account management + dashboard overview; Admin-gated server-side) ----
export interface AdminDoc {
  doc_id: string; title: string; group: string | null; in_kb: boolean;
  extracted: boolean; status: string; kb_fields: number; populated: number; verified: number;
  doc_type: string | null; doc_date: string | null; block_id: string | null;
  relation?: string | null; parent_doc_id?: string | null;
}
export interface AdminJob { status: string; kind?: string; stage?: string; error?: string | null; }
export interface AdminTotals {
  documents: number; extracted: number; in_kb: number; populated: number; verified: number;
  accounts: number; verifiers: number; ops_fields: number; parties: number; families: number;
  human_validated: number; min_approvals: number; correction_approvals: number;
}
export interface AdminOverview { totals: AdminTotals; documents: AdminDoc[]; }

export async function getAdminOverview(): Promise<AdminOverview> {
  const r = await apiFetch(apiUrl("/admin/overview"), { headers: authHeaders() });
  if (!r.ok) throw new Error(`/admin/overview: ${r.status}`);
  return r.json();
}

// ---- Danger zone: reset this instance back to a fresh install ----
export interface DevResetStatus { enabled: boolean; confirm_phrase: string; }
export async function getDevResetStatus(): Promise<DevResetStatus> {
  const r = await apiFetch(apiUrl("/admin/dev-reset"), { headers: authHeaders() });
  if (!r.ok) return { enabled: false, confirm_phrase: "RESET" };
  return r.json();
}
export async function performDevReset(confirm: string, wipeKey: boolean): Promise<void> {
  const r = await apiFetch(apiUrl("/admin/dev-reset"), {
    method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ confirm, wipe_key: wipeKey }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(String((data as { detail?: string }).detail || `reset: ${r.status}`));
}

// ---- Usage metrics (who uses the app, how much: logins + token spend) ----
export interface UsageUser {
  email: string; name: string; role: string;
  questions: number; prompt_tokens: number; completion_tokens: number;
  logins: number; last_login: string | null; last_active: string | null;
}
export interface UsageDay { day: string; questions: number; tokens: number; logins: number; }
export interface UsageUserDay { day: string; questions: number; tokens: number; }
/** One user's dense 7-day series (zero-filled server-side) + window totals. */
export interface UsageUserSeries {
  email: string; name: string; questions: number; tokens: number;
  series: UsageUserDay[];
}
export interface UsageMetrics {
  totals: {
    questions: number; prompt_tokens: number; completion_tokens: number;
    logins: number; active_users_14d: number;
  };
  users: UsageUser[];
  daily: UsageDay[];
  /** Dense last-7-days axis for the combo charts (bars=questions, line=tokens). */
  daily7: UsageDay[];
  user_daily: UsageUserSeries[];
}
export async function getUsageMetrics(): Promise<UsageMetrics> {
  const r = guard401(await apiFetch(apiUrl("/admin/metrics"), { headers: authHeaders() }));
  if (!r.ok) throw new Error(`/admin/metrics: ${r.status}`);
  return r.json();
}

// ---- Activity feed (the admin changelog: who did what, when) ----
export type ActivityKind = "verification" | "ontology" | "account" | "document" | "feedback"
  | "confidential_access" | "other";
export interface ActivityItem {
  at: string; actor: string; actor_name: string;
  kind: ActivityKind; action: string; target: string; detail: string;
}
export async function getActivity(limit = 200): Promise<ActivityItem[]> {
  const r = guard401(await apiFetch(apiUrl(`/admin/activity?limit=${limit}`), { headers: authHeaders() }));
  if (!r.ok) throw new Error(`/admin/activity: ${r.status}`);
  const { items } = await r.json();
  return items ?? [];
}

// ---- User feedback (any logged-in user files; admins triage) ----
export type FeedbackCategory = "ui_ux" | "extraction" | "answer" | "other";
export type FeedbackStatus = "new" | "acknowledged" | "fixed";
export interface FeedbackItem {
  id: number; created_at: string; email: string; category: FeedbackCategory;
  message: string; context: Record<string, unknown> | null;
  status: FeedbackStatus; status_by: string | null; status_at: string | null;
  attachments?: string[] | null;
}
export async function postFeedback(body: {
  category: FeedbackCategory; message: string; context?: Record<string, unknown>;
  attachments?: string[];
}): Promise<void> {
  const r = guard401(await apiFetch(apiUrl("/feedback"), {
    method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  }));
  if (!r.ok) throw new Error(`feedback: ${r.status}`);
}
/** Upload feedback screenshots first; the report then references the returned
 * server names. Images only, at most 3, 8 MB each (validated server-side). */
export async function uploadFeedbackImages(files: File[]): Promise<string[]> {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  const r = guard401(await apiFetch(apiUrl("/feedback/upload"), {
    method: "POST", headers: authHeaders(), body: form,
  }));
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(String((data as { detail?: string }).detail || `upload: ${r.status}`));
  return (data as { names: string[] }).names ?? [];
}
/** Admin-gated screenshot URL (fetched with auth headers via AuthedImage). */
export const feedbackAttachmentUrl = (name: string): string =>
  apiUrl(`/feedback/admin/attachment/${encodeURIComponent(name)}`);
export async function getFeedback(status?: FeedbackStatus | null): Promise<{
  items: FeedbackItem[]; counts: Record<FeedbackStatus, number>;
}> {
  const q = status ? `?status=${status}` : "";
  const r = guard401(await apiFetch(apiUrl(`/feedback/admin${q}`), { headers: authHeaders() }));
  if (!r.ok) throw new Error(`feedback list: ${r.status}`);
  return r.json();
}
/** The logged-in user's own recent reports with triage status (loop closure). */
export interface MyFeedbackItem {
  id: number; created_at: string; category: FeedbackCategory;
  message: string; status: FeedbackStatus;
}
export async function getMyFeedback(): Promise<MyFeedbackItem[]> {
  const r = guard401(await apiFetch(apiUrl("/feedback/mine"), { headers: authHeaders() }));
  if (!r.ok) return [];
  const { items } = await r.json();
  return items ?? [];
}
export async function setFeedbackStatus(id: number, status: FeedbackStatus): Promise<void> {
  const r = guard401(await apiFetch(apiUrl(`/feedback/admin/${id}`), {
    method: "PUT", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ status }),
  }));
  if (!r.ok) throw new Error(`feedback status: ${r.status}`);
}
export async function setAccountRole(email: string, role: string): Promise<void> {
  const r = await apiFetch(apiUrl(`/auth/accounts/${encodeURIComponent(email)}`), {
    method: "PUT", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ role }),
  });
  if (!r.ok) throw new Error(`set role: ${r.status}`);
}
/** Partial account edit (admin). `new_email` renames the account — its sessions and
 * chat history follow, so the person keeps their login and conversations.
 * `verifier` grants/revokes the approved-verifier flag (the right to vote on
 * field verifications in the Review tab). */
export async function editAccount(
  email: string,
  patch: { name?: string; title?: string; new_email?: string; role?: string; verifier?: boolean },
): Promise<void> {
  const r = await apiFetch(apiUrl(`/auth/accounts/${encodeURIComponent(email)}`), {
    method: "PUT", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(patch),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(String((data as { detail?: string }).detail || `edit account: ${r.status}`));
}
export async function addAccount(body: { email: string; name?: string; title?: string; role?: string; zone?: string }): Promise<void> {
  const r = await apiFetch(apiUrl("/auth/accounts"), {
    method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(String((data as { detail?: string }).detail || `add account: ${r.status}`));
}

/** What the uploader DECLARES about a document at upload: what kind of
 * document it is, its date, and how it relates to an existing contract.
 * Human input — it drives the family chain; extraction only cross-checks it. */
export interface UploadIntake {
  relation?: "standalone" | "amends" | "novates" | "supersedes";
  parent_doc_id?: string;
  document_type?: string;
  document_date?: string;
  /** Folder to file the upload into. */
  folder_id?: string;
}

/** Upload a PDF (admin) together with its declared intake. The server stores
 * both and runs the extraction chain in the background. */
export async function uploadDocument(
  file: File, intake: UploadIntake = {},
): Promise<{ doc_id: string; title: string; already_known: boolean; extracting: boolean }> {
  const form = new FormData();
  form.append("file", file);
  if (intake.relation) form.append("relation", intake.relation);
  if (intake.parent_doc_id) form.append("parent_doc_id", intake.parent_doc_id);
  if (intake.document_type) form.append("document_type", intake.document_type);
  if (intake.document_date) form.append("document_date", intake.document_date);
  if (intake.folder_id) form.append("folder_id", intake.folder_id);
  const r = await apiFetch(apiUrl("/admin/upload"), { method: "POST", headers: authHeaders(), body: form });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(String((data as { detail?: string }).detail || `upload: ${r.status}`));
  return data;
}

/** Run the paid extraction chain for one document (ingest → fields → review → KB). */
export async function extractDocument(docId: string): Promise<void> {
  const r = await apiFetch(apiUrl(`/admin/extract/${encodeURIComponent(docId)}`), {
    method: "POST", headers: authHeaders(),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(String((data as { detail?: string }).detail || `extract: ${r.status}`));
}
export async function getAdminJobs(): Promise<Record<string, AdminJob>> {
  const r = await apiFetch(apiUrl("/admin/jobs"), { headers: authHeaders() });
  if (!r.ok) return {};
  const { jobs } = await r.json();
  return jobs ?? {};
}

// ---- Per-user chat history (persisted server-side, keyed to the account) ----
export async function getThreadStore(): Promise<string | null> {
  const r = await apiFetch(apiUrl("/threads"), { headers: authHeaders() });
  if (!r.ok) return null;
  const { data } = await r.json();
  return data ?? null;
}
export async function putThreadStore(data: string): Promise<void> {
  try {
    await apiFetch(apiUrl("/threads"), {
      method: "PUT", headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ data }),
    });
  } catch { /* offline — localStorage cache still holds it */ }
}

/** Access policy (RBAC). GET current state; PUT the labels to hide in
 * Restricted view. In-memory on the server — applies live. */
export async function getPolicy(): Promise<PolicyState> {
  const r = await apiFetch(apiUrl("/policy"), { headers: authHeaders() });
  if (!r.ok) throw new Error(`/policy: ${r.status}`);
  return r.json();
}

export async function updatePolicy(hiddenLabels: string[]): Promise<PolicyState> {
  const r = await apiFetch(apiUrl("/policy"), {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ hidden_labels: hiddenLabels }),
  });
  if (!r.ok) throw new Error(`/policy PUT: ${r.status}`);
  return r.json();
}

export async function listDocuments(): Promise<DocumentMeta[]> {
  const r = await apiFetch(apiUrl("/documents"), { headers: authHeaders() });
  if (!r.ok) throw new Error(`/documents: ${r.status}`);
  const { docs } = await r.json();
  return docs;
}

/** Blur regions for a document under the SESSION role (server-derived). The PDF
 * viewer overlays these so a sensitive value can't be read off the source page
 * even when no answer cited it. Empty for a cleared role. */
export async function getRestrictedRegions(docId: string): Promise<CitationRect[]> {
  const r = guard401(await fetch(
    apiUrl(`/restricted-regions/${encodeURIComponent(docId)}`),
    { headers: authHeaders() },
  ));
  if (!r.ok) return [];
  const { regions } = await r.json();
  return regions ?? [];
}

export async function getEvidence(evidenceId: string): Promise<EvidenceDetail> {
  const r = await apiFetch(apiUrl(`/evidence/${encodeURIComponent(evidenceId)}`), {
    headers: authHeaders(),
  });
  if (!r.ok) throw new Error(`/evidence: ${r.status}`);
  return r.json();
}

// ── Review / verification (human-in-the-loop, multi-verifier votes) ─────────
export interface ReviewEvidence { snippet: string; page: number | null; rects: string | null; kind: string; }
export interface ReviewValue { value: unknown; n_mentions: number; evidence: ReviewEvidence[]; }
/** One verifier's standing vote on a field. `value` null = endorses the machine
 * value; set = proposes/concurs with that correction. */
export interface ReviewVote {
  voter: string; voter_name?: string; decision: "approve" | "reject";
  value: string | null; comment: string | null; at: string;
  has_evidence?: boolean;
}
/** The clause a winning correction carried (replaces the machine anchor). */
export interface CorrectionEvidence {
  snippet?: string | null; page?: number | null; rects?: string | null;
}
export interface ReviewField {
  title: string; category: string; type: string; method: string;
  multiplicity: number; sensitivity: string; status: string;
  conflict: boolean; verified: boolean; verified_value?: string | null;
  verified_evidence?: CorrectionEvidence | null;
  verifier?: string | null; can_edit?: boolean; values: ReviewValue[];
  /** Consensus of the standing votes: confidence = largest vote share. */
  disputed?: boolean; confidence?: number | null; n_votes?: number;
  needs_correction?: boolean; pending_value?: string | null;
  verifiers?: string[]; votes?: ReviewVote[]; my_vote?: ReviewVote | null;
}
export interface ReviewDocRecord {
  doc_id: string; title: string; role: string; can_edit: boolean;
  hidden_fields: number; status_counts: Record<string, number>;
  quorum?: number; correction_quorum?: number;
  fields: Record<string, ReviewField>;
}
export interface ReviewDocSummary {
  doc_id: string; title: string; status_counts: Record<string, number>;
  n_fields: number; n_visible: number; n_populated: number; n_verified: number;
  n_needs_correction?: number;
  /** Contract family (the folder id) and its display name, for folder roll-ups. */
  group?: string | null; group_name?: string | null;
}

export async function listReviewDocs(): Promise<ReviewDocSummary[]> {
  const r = guard401(await apiFetch(apiUrl("/review/docs"), { headers: authHeaders() }));
  if (!r.ok) throw new Error(`/review/docs: ${r.status}`);
  const { docs } = await r.json();
  return docs;
}

export async function getReviewDoc(docId: string): Promise<ReviewDocRecord> {
  const r = guard401(await fetch(
    apiUrl(`/review/doc/${encodeURIComponent(docId)}`),
    { headers: authHeaders() }));
  if (!r.ok) throw new Error(`/review/doc: ${r.status}`);
  return r.json();
}

/** Cast (or change) the logged-in verifier's vote on a field. approve with no
 * value endorses the machine value; approve with a value proposes/concurs with
 * a correction (optionally carrying the clause that backs it); reject flags
 * the value as wrong. */
export async function voteReviewField(
  docId: string, fieldKey: string,
  body: {
    decision: "approve" | "reject"; value?: string | null; comment?: string;
    snippet?: string; page?: number; rects?: string;
  },
): Promise<void> {
  const r = await fetch(
    apiUrl(`/review/doc/${encodeURIComponent(docId)}/field/${encodeURIComponent(fieldKey)}/verify`),
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({
        decision: body.decision, value: body.value ?? null, comment: body.comment ?? null,
        snippet: body.snippet ?? null, page: body.page ?? null, rects: body.rects ?? null,
      }),
    });
  if (!r.ok) throw new Error(`vote: ${r.status}`);
}

export const reviewPageUrl = (docId: string, page: number): string =>
  apiUrl(`/review/page/${encodeURIComponent(docId)}/${page}`);

/** Find text in a document's OCR blocks (review viewer's find box). Server-side
 * because the review canvas shows page images, not a PDF text layer. */
export interface ReviewSearchMatch {
  page: number; bbox: [number, number, number, number]; snippet: string;
}
export async function searchReviewDoc(docId: string, q: string): Promise<ReviewSearchMatch[]> {
  const r = guard401(await apiFetch(
    apiUrl(`/review/search/${encodeURIComponent(docId)}?q=${encodeURIComponent(q)}`),
    { headers: authHeaders() }));
  if (!r.ok) return [];
  const { matches } = await r.json();
  return matches ?? [];
}

// ---- Evidence corrections (reviewer fixes the machine's citations) ----
export interface ReviewBlock { bbox: [number, number, number, number]; text: string; }
export async function getReviewBlocks(docId: string, page: number): Promise<ReviewBlock[]> {
  const r = await apiFetch(apiUrl(`/review/blocks/${encodeURIComponent(docId)}/${page}`), { headers: authHeaders() });
  if (!r.ok) return [];
  const { blocks } = await r.json();
  return blocks ?? [];
}
export async function rejectReviewEvidence(
  docId: string, fieldKey: string, page: number | null, snippet: string,
  rects: string | null, value: string | null,
): Promise<void> {
  const r = await fetch(
    apiUrl(`/review/doc/${encodeURIComponent(docId)}/field/${encodeURIComponent(fieldKey)}/reject-evidence`),
    { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ page, snippet, rects, value }) });
  if (!r.ok) throw new Error(`reject evidence: ${r.status}`);
}
export async function attachReviewEvidence(
  docId: string, fieldKey: string,
  body: { value: string; snippet: string; page: number | null; rects: string | null },
): Promise<void> {
  const r = await fetch(
    apiUrl(`/review/doc/${encodeURIComponent(docId)}/field/${encodeURIComponent(fieldKey)}/attach-evidence`),
    { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`attach evidence: ${r.status}`);
}

// Review overview heatmap — documents × categories, coloured by verified/populated.
export interface HeatmapCell { populated: number; verified: number; }
export interface HeatmapDoc {
  doc_id: string; title: string; n_populated: number; n_verified: number;
  cells: Record<string, HeatmapCell>;
}
export interface ReviewHeatmap {
  role: string; categories: { key: string; title: string }[]; docs: HeatmapDoc[];
}
export async function getReviewHeatmap(): Promise<ReviewHeatmap> {
  const r = guard401(await apiFetch(apiUrl("/review/heatmap"), { headers: authHeaders() }));
  if (!r.ok) throw new Error(`/review/heatmap: ${r.status}`);
  return r.json();
}

// ---- Ontology CRUD (the schema-first field definitions + RBAC sensitivity) ----
export interface OntologyField {
  key: string; full_key: string; title: string; type: string; values: string[];
  hint: string; sensitivity: string; mechanism: string; multiplicity: number;
  origin: string; user_field: boolean;
  /** Who last edited this field through the app (null for untouched base fields). */
  updated_by?: string | null; updated_at?: string | null;
}
export interface OntologyCategory {
  key: string; title: string; level: string; fields: OntologyField[];
}
export interface OntologyResponse {
  categories: OntologyCategory[]; default_level: string; can_edit: boolean;
}
export async function getOntology(): Promise<OntologyResponse> {
  const r = await apiFetch(apiUrl("/ontology"), { headers: authHeaders() });
  if (!r.ok) throw new Error(`/ontology: ${r.status}`);
  return r.json();
}
export async function setCategorySensitivity(category: string, level: string): Promise<void> {
  const r = await apiFetch(apiUrl("/ontology/sensitivity"), {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ category, level }),
  });
  if (!r.ok) throw new Error(`/ontology/sensitivity: ${r.status}`);
}

/** Add a USER field — the schema-first extractor fills it from its hint (mechanism: llm). */
export interface NewFieldBody {
  category: string; key: string; title: string; type: string;
  hint?: string; values?: string[]; multiplicity?: number;
}
async function _ontologyMutate(path: string, method: string, body?: unknown): Promise<Record<string, unknown>> {
  const r = await apiFetch(apiUrl(path), {
    method,
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(String((data as { detail?: string }).detail || `${path}: ${r.status}`));
  return data as Record<string, unknown>;
}
export function addOntologyField(body: NewFieldBody): Promise<Record<string, unknown>> {
  return _ontologyMutate("/ontology/field", "POST", body);
}
export interface EditFieldBody {
  title?: string; hint?: string; values?: string[]; sensitivity?: string;
}
export function editOntologyField(category: string, key: string, body: EditFieldBody): Promise<Record<string, unknown>> {
  return _ontologyMutate(
    `/ontology/field/${encodeURIComponent(category)}/${encodeURIComponent(key)}`, "PUT", body);
}
export function deleteOntologyField(category: string, key: string): Promise<Record<string, unknown>> {
  return _ontologyMutate(
    `/ontology/field/${encodeURIComponent(category)}/${encodeURIComponent(key)}`, "DELETE");
}

// ---- Knowledge Base (the aligned KM layer: current-per-field + supersedence) ----
export interface KmDoc { doc_id: string; title: string; date: string | null; n_fields: number; }
export interface KmFamily { group: string; title: string; n_docs: number; n_fields: number; docs: KmDoc[]; }
export interface KmStatement {
  value: string; values: string[]; unit: string | null; trust: string; verified: boolean;
  confidence?: number | null; verifiers?: string[] | null; n_votes?: number | null; disputed?: boolean;
  doc_id: string; doc_title: string; page: number | null; snippet: string | null; rects: string | null;
}
export interface KmField {
  full_key: string; field_key: string; category: string; title: string; type: string;
  sensitivity: string; multiplicity: number; current: KmStatement[]; superseded: KmStatement[];
}
export interface KmCategory { category: string; fields: KmField[]; }
export interface KmDagEdge { src: string; rel: string; tgt: string; }
export interface KmParty {
  name: string; key: string; roles: string[];
  /** true = the family's CURRENT customer, false = a former (novated-away)
   * customer, null/undefined = no currency knowledge for this party. */
  current?: boolean | null;
}
export interface KmConflict { doc_id: string; doc_title: string; reason: string; }
export interface KmAlignedView {
  group: string; role: string; parties: KmParty[]; dag: KmDagEdge[]; categories: KmCategory[];
  conflicts?: KmConflict[];
  hidden_fields: number; n_fields: number; n_current_statements: number; n_verified: number;
}
export interface KmAggregate {
  field_key: string; group: string | null; op: string; n_docs: number; n_hidden: number;
  items: { doc_id: string; title: string; value: string; numbers: number[]; unit: string | null }[];
  sum?: number; n_numbers?: number;
}

export async function getKmFamilies(): Promise<KmFamily[]> {
  const r = guard401(await apiFetch(apiUrl("/km/families"), { headers: authHeaders() }));
  if (!r.ok) throw new Error(`/km/families: ${r.status}`);
  const { families } = await r.json();
  return families;
}
export async function getKmFamily(group: string): Promise<KmAlignedView> {
  const r = guard401(await fetch(
    apiUrl(`/km/family/${encodeURIComponent(group)}`),
    { headers: authHeaders() }));
  if (!r.ok) throw new Error(`/km/family: ${r.status}`);
  return r.json();
}
export async function getKmAggregate(
  fieldKey: string, group: string | undefined, op = "sum",
): Promise<KmAggregate> {
  const parts = [`field_key=${encodeURIComponent(fieldKey)}`, `op=${op}`];
  if (group) parts.push(`group=${encodeURIComponent(group)}`);
  const r = await apiFetch(apiUrl(`/km/aggregate?${parts.join("&")}`), { headers: authHeaders() });
  if (!r.ok) throw new Error(`/km/aggregate: ${r.status}`);
  return r.json();
}

/** Page image for the Knowledge Base evidence pane (clearance-gated route). */
export const kmPageUrl = (docId: string, page: number): string =>
  apiUrl(`/km/page/${encodeURIComponent(docId)}/${page}`);

/** Download the aligned knowledge as an Excel workbook (fetched with auth
 * headers, then handed to the browser as a normal file download). */
export async function downloadKmExport(group?: string): Promise<void> {
  const qs = group ? `?group=${encodeURIComponent(group)}` : "";
  const r = guard401(await apiFetch(apiUrl(`/km/export${qs}`), { headers: authHeaders() }));
  if (!r.ok) throw new Error(`/km/export: ${r.status}`);
  const blob = await r.blob();
  const cd = r.headers.get("Content-Disposition") || "";
  const m = cd.match(/filename="([^"]+)"/);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = m?.[1] || "knowledge_export.xlsx";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
}

export async function getSubgraph(evidenceIds: string[]): Promise<GraphPayload> {
  if (evidenceIds.length === 0) return { nodes: [], edges: [] };
  const qs = evidenceIds
    .map(id => `evidence_ids=${encodeURIComponent(id)}`)
    .join("&");
  const r = await apiFetch(apiUrl(`/graph/subgraph?${qs}`), { headers: authHeaders() });
  if (!r.ok) throw new Error(`/graph/subgraph: ${r.status}`);
  return r.json();
}

export async function getOverviewGraph(opts: {
  docId?: string;
  labels?: string[];
  limit?: number;
}): Promise<GraphPayload> {
  const parts: string[] = [];
  if (opts.docId) parts.push(`doc_id=${encodeURIComponent(opts.docId)}`);
  for (const l of opts.labels ?? []) {
    parts.push(`labels=${encodeURIComponent(l)}`);
  }
  if (opts.limit) parts.push(`limit_nodes=${opts.limit}`);
  const r = await apiFetch(apiUrl(`/graph/overview?${parts.join("&")}`), {
    headers: authHeaders(),
  });
  if (!r.ok) throw new Error(`/graph/overview: ${r.status}`);
  return r.json();
}

/** Cross-doc identity lens: Documents and the identity hubs their facts
 * resolve to. A dedicated endpoint (not a filter of /overview) so a hub
 * spanning two documents is never clipped by the overview fact cap. */
export async function getHubGraph(docId?: string): Promise<GraphPayload> {
  const qs = docId ? `?doc_id=${encodeURIComponent(docId)}` : "";
  const r = await apiFetch(apiUrl(`/graph/hubs${qs}`), { headers: authHeaders() });
  if (!r.ok) throw new Error(`/graph/hubs: ${r.status}`);
  return r.json();
}

/** SSE chat consumer.
 *
 * We use POST with EventSourceResponse on the server. EventSource itself is
 * GET-only, so we implement the SSE parser by hand over fetch+ReadableStream.
 *
 * Calls handlers with the parsed event payloads. Returns an `abort` handle
 * the caller can wire to a cancel button.
 */
export interface AgentStepPayload {
  step: number;
  thought: string;
  tool_calls: { name: string; args: Record<string, unknown> }[];
}

export interface AgentToolCallPayload {
  step: number;
  tool_call_id: string;
  tool: string;
  args: Record<string, unknown>;
}

export interface AgentToolResultPayload {
  step: number;
  tool_call_id: string;
  tool: string;
  n_results: number;
  n_new: number;
  n_total: number;
  count: number | null;
  error: string | null;
}

export interface AgentDonePayload {
  steps_used: number;
  finish_reason: string;
  n_citations: number;
}

export interface ChatHandlers {
  onPlan?: (plan: IntentPlan) => void;
  onAgentStep?: (s: AgentStepPayload) => void;
  onAgentToolCall?: (t: AgentToolCallPayload) => void;
  onAgentToolResult?: (r: AgentToolResultPayload) => void;
  onAgentDone?: (d: AgentDonePayload) => void;
  onCitations?: (citations: Citation[]) => void;
  onToken?: (delta: string) => void;
  onDone?: (info: { used_citations: string[]; unknown_citations: string[] }) => void;
  onError?: (msg: string) => void;
}

export function streamChat(
  messages: ChatMessage[],
  handlers: ChatHandlers,
  opts: { docIds?: string[]; role?: string } = {},
): { abort: () => void } {
  const ctrl = new AbortController();

  (async () => {
    try {
      const resp = await apiFetch(apiUrl("/chat"), {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        signal: ctrl.signal,
        body: JSON.stringify({
          messages,
          doc_ids: opts.docIds ?? null,
          // Admin-only impersonation override; the server uses the SESSION
          // role for everyone else (opts.role is normally undefined).
          role: opts.role ?? null,
        }),
      });
      if (!resp.ok || !resp.body) {
        if (resp.status === 401) notifyAuthExpired();
        handlers.onError?.(resp.status === 401
          ? "Your session ended. Sign in again to continue."
          : WARMING_STATUS.has(resp.status)
            ? "The server is waking up from sleep. Wait about half a minute, then send your question again."
            : `HTTP ${resp.status}`);
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      // SSE events are separated by a blank line. Match either LF/LF or
      // CRLF/CRLF — sse-starlette uses os.linesep, so on Windows the
      // boundary arrives as \r\n\r\n. Splitting only on \n\n misses it
      // entirely and the stream looks frozen client-side.
      const EVENT_BOUNDARY = /\r?\n\r?\n/;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        for (;;) {
          const m = buffer.match(EVENT_BOUNDARY);
          if (!m || m.index === undefined) break;
          const raw = buffer.slice(0, m.index);
          buffer = buffer.slice(m.index + m[0].length);
          dispatch(raw, handlers);
        }
      }
    } catch (err: unknown) {
      if ((err as Error).name === "AbortError") return;
      handlers.onError?.((err as Error).message || "stream failed");
    }
  })();

  return { abort: () => ctrl.abort() };
}

function dispatch(raw: string, handlers: ChatHandlers): void {
  let event = "message";
  let data = "";
  // Handle both LF and CRLF line endings within a single event frame.
  for (const line of raw.split(/\r?\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) {
      data += (data ? "\n" : "") + line.slice(5).trimStart();
    }
  }
  if (!data) return;
  let payload: unknown;
  try {
    payload = JSON.parse(data);
  } catch {
    return;
  }
  switch (event) {
    case "plan":
      handlers.onPlan?.(payload as IntentPlan);
      break;
    case "step":
      handlers.onAgentStep?.(payload as AgentStepPayload);
      break;
    case "tool_call":
      handlers.onAgentToolCall?.(payload as AgentToolCallPayload);
      break;
    case "tool_result":
      handlers.onAgentToolResult?.(payload as AgentToolResultPayload);
      break;
    case "agent_done":
      handlers.onAgentDone?.(payload as AgentDonePayload);
      break;
    case "citations":
      handlers.onCitations?.((payload as { citations: Citation[] }).citations);
      break;
    case "token":
      handlers.onToken?.((payload as { delta: string }).delta);
      break;
    case "done":
      handlers.onDone?.(payload as { used_citations: string[]; unknown_citations: string[] });
      break;
    case "error":
      handlers.onError?.((payload as { message: string }).message);
      break;
  }
}

// ---- Folders (named contract families, the folder_id IS the group string) ----
export interface RegistryFolder {
  folder_id: string; name: string;
  active: number; updated_by: string | null; updated_at: string | null;
  doc_ids: string[]; n_docs: number;
}
export async function getRegistryFolders(): Promise<RegistryFolder[]> {
  const r = await apiFetch(apiUrl("/registry/folders"), { headers: authHeaders() });
  if (!r.ok) throw new Error(`/registry/folders: ${r.status}`);
  const { folders } = await r.json();
  return folders ?? [];
}
export async function createRegistryFolder(body: {
  name: string;
}): Promise<{ folder_id: string }> {
  const r = await apiFetch(apiUrl("/registry/folders"), {
    method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(String((data as { detail?: string }).detail || `create folder: ${r.status}`));
  return data as { folder_id: string };
}
export async function patchRegistryFolder(
  folderId: string,
  patch: { name?: string; active?: boolean },
): Promise<void> {
  const r = await apiFetch(apiUrl(`/registry/folders/${encodeURIComponent(folderId)}`), {
    method: "PATCH", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(patch),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(String((data as { detail?: string }).detail || `update folder: ${r.status}`));
}
/** Move a document into a folder (or out of every folder with null). Sets the
 * declared family and refreshes the knowledge base for free. */
export async function moveDocumentFolder(docId: string, folderId: string | null): Promise<void> {
  const r = await apiFetch(apiUrl(`/admin/doc/${encodeURIComponent(docId)}/folder`), {
    method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ folder_id: folderId }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(String((data as { detail?: string }).detail || `move document: ${r.status}`));
}
// ---- First-run setup wizard (no session exists yet — no auth headers) ----
export interface SetupStatus {
  complete: boolean;
  has_domain: boolean;
  domain: string;
  available_domains: string[];
  has_admin: boolean;
  admin_email: string | null;
  bootstrap_email_pending: boolean;
  has_api_key: boolean;
  app_name: string;
}
async function _setupCall(path: string, body?: unknown, method = "POST"): Promise<Record<string, unknown>> {
  const r = await apiFetch(apiUrl(path), {
    method, headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(String((data as { detail?: string }).detail || `${path}: ${r.status}`));
  return data as Record<string, unknown>;
}
export async function getSetupStatus(): Promise<SetupStatus> {
  const r = await apiFetch(apiUrl("/setup/status"));
  if (!r.ok) throw new Error(`/setup/status: ${r.status}`);
  return r.json();
}
export const setSetupInstance = (name: string) => _setupCall("/setup/instance", { name });
export const setSetupAdmin = (email: string, name: string) =>
  _setupCall("/setup/admin", { email, name });
export const testSetupApiKey = (key: string, base_url = "") =>
  _setupCall("/setup/api-key", { key, base_url });
export async function getSetupDomains(): Promise<string[]> {
  const r = await apiFetch(apiUrl("/setup/domains"));
  if (!r.ok) throw new Error(`/setup/domains: ${r.status}`);
  const { domains } = await r.json();
  return domains ?? [];
}
export const chooseDemoDomain = () => _setupCall("/setup/domain/demo");
export const chooseExistingDomain = (domain: string) =>
  _setupCall("/setup/domain/use-existing", { domain });
export const activateSetupDomain = () => _setupCall("/setup/domain/activate");
export const finishSetup = () => _setupCall("/setup/finish");

// Clone-and-edit (Phase 2): the same OntologyEditor component the admin
// Ontology tab uses, wired to these unauthenticated passthrough routes
// instead of /ontology/* — there is no session yet mid-wizard. See
// api/routes/setup.py's "Clone-and-edit" section for the backend side.
export async function getSetupOntology(): Promise<OntologyResponse> {
  const r = await apiFetch(apiUrl("/setup/ontology"));
  if (!r.ok) throw new Error(`/setup/ontology: ${r.status}`);
  return r.json();
}
export const addSetupOntologyField = (body: NewFieldBody) =>
  _setupCall("/setup/ontology/field", body, "POST");
export const editSetupOntologyField = (category: string, key: string, body: EditFieldBody) =>
  _setupCall(`/setup/ontology/field/${encodeURIComponent(category)}/${encodeURIComponent(key)}`, body, "PUT");
export const deleteSetupOntologyField = (category: string, key: string) =>
  _setupCall(`/setup/ontology/field/${encodeURIComponent(category)}/${encodeURIComponent(key)}`, undefined, "DELETE");
export const setSetupCategorySensitivity = (category: string, level: string) =>
  _setupCall("/setup/ontology/sensitivity", { category, level }, "PUT");

// Build from scratch (Phase 3): a plain-language description drafts a field
// list (one real, paid model call), the rest is edited entirely client-side
// (see SetupWizard.tsx's local field-list state) until Generate assembles
// and validates the four real config files in one call.
export interface DraftedField {
  key: string; title: string; type: string; hint: string;
  values: string[]; category: string; confidential: boolean;
}
export async function draftSchema(description: string): Promise<DraftedField[]> {
  const data = await _setupCall("/setup/schema/draft", { description });
  return (data.fields as DraftedField[] | undefined) ?? [];
}
export interface ScratchField {
  key: string; title: string; type: string; hint: string;
  values?: string[]; category: string; multiplicity?: number; confidential?: boolean;
}
export async function buildFromScratch(body: {
  domain: string;
  fields: ScratchField[];
  amendment?: { enabled: boolean; document_types?: string[] };
  party?: { enabled: boolean; field_keys?: string[] };
}): Promise<{ ok: boolean; domain: string }> {
  const data = await _setupCall("/setup/domain/from-scratch", body);
  return data as unknown as { ok: boolean; domain: string };
}

/** Partial edit of a document's declared intake after upload. Only the keys
 * sent change. The merged declaration is validated with the upload rules. */
export async function updateDocumentIntake(docId: string, patch: UploadIntake): Promise<void> {
  const r = await apiFetch(apiUrl(`/admin/doc/${encodeURIComponent(docId)}/intake`), {
    method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(patch),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(String((data as { detail?: string }).detail || `update intake: ${r.status}`));
}
