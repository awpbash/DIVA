// Mirror of api/rag/schemas.py — kept hand-maintained so the frontend has
// crisp types without an OpenAPI codegen step. If you change a field on
// either side, change it here too.

export type Intent =
  | "factual" | "comparison" | "formula" | "clause"
  | "cross_ref" | "aggregation" | "definition" | "other";

export interface IntentPlan {
  intent: Intent;
  key_terms: string[];
  categories: string[];
  needs_table: boolean;
  rationale: string;
}

export interface CitationRect {
  page_no: number;
  bbox: [number, number, number, number];
}

export interface Citation {
  evidence_id: string;
  /** "evidence_span" | "reference" | "fact_mention" | "block" |
   * "section_heading" | "document_asset" (a full-page diagram — page-level
   * pointer, no rects: the viewer just opens the page).
   * Present on chat citations; absent on ones synthesised from /evidence. */
  citation_kind?: string | null;
  /** Trust tier: "canonical" | "reference" | "raw" | "layout". */
  confidence_tier?: string | null;
  doc_id: string;
  page_no: number;
  /** Per-page rects, one per cited block/table-row. Preferred over the
   * envelope `bbox` — a fact cited in two far-apart blocks draws two
   * tight rectangles instead of one giant box. */
  rects?: CitationRect[] | null;
  section_id: string | null;
  section_num: string | null;
  section_title: string | null;
  snippet: string;
  bbox: [number, number, number, number] | null;
  fact_label: string | null;
  fact_id: string | null;
  fact_summary: string | null;
  score: number;
  /** Access-policy tags (api/rag/policy.py). `sensitivity` is the class this
   * evidence belongs to (e.g. "financial"); `restricted` is true when the
   * role the answer was generated under may not see it. The UI blurs sensitive
   * evidence while the global "Restricted view" toggle is on. */
  sensitivity?: string | null;
  restricted?: boolean;
  /** Document review trust ("referencing reviewed docs is reliable"): tier is
   * "reviewed" | "partial" | "unreviewed"; the counts back the tooltip. */
  doc_trust?: string;
  doc_verified?: number;
  doc_populated?: number;
  /** Field-level verification consensus (knowledge-base citations only):
   * who stands behind this exact value and the winning vote share. */
  field_trust?: "human_validated" | "disputed" | null;
  verified_by?: string[] | null;
  verify_confidence?: number | null;
  verify_votes?: number | null;
}

/** Access-policy admin state (GET/PUT /policy). Drives the settings panel. */
export interface PolicyState {
  available_labels: string[];
  hidden_labels: string[];
  restricted_role: string;
  roles: string[];
}

export interface DocumentMeta {
  doc_id: string;
  title: string;
  doctype: string | null;
  total_pages: number;
  n_facts: number;
  has_pdf: boolean;
  /** Contract family (shared `group` across base + amendments); null = standalone. */
  group?: string | null;
  /** Folder display name for the family (from the folder registry). Falls
   * back to the raw group string when no folder row names it. */
  group_name?: string | null;
  /** The document's own stated type ("Base Agreement", "Supplemental Agreement"…). */
  doc_type?: string | null;
  /** The document's stated date as written ("17 October 2022") and as an ISO sort key. */
  doc_date?: string | null;
  doc_date_iso?: string | null;
  /** Review trust — the same status the heatmap shows, surfaced in the picker:
   * "reviewed" (all stated fields verified) | "partial" | "unreviewed". */
  review_tier?: string;
  review_verified?: number;
  review_populated?: number;
}

export interface EvidenceDetail {
  evidence_id: string;
  doc_id: string;
  page_no: number;
  snippet: string;
  bbox: [number, number, number, number] | null;
  /** Per-page rects (one per cited block/table-row) — preferred over bbox by
   * the PDF viewer. Present on canonical EvidenceSpans loaded with geometry. */
  rects?: CitationRect[] | null;
  section_id: string | null;
  section_num: string | null;
  section_title: string | null;
  fact_label: string | null;
  fact_props: Record<string, unknown> | null;
}

export interface GraphNode {
  id: string;
  label: string;
  title: string;
  props: Record<string, unknown>;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type: string;
  /** Edge-borne explainability (grounding method / confidence / signals,
   * derivation provenance). Empty {} for plain structural edges. */
  props?: Record<string, unknown>;
}

export interface GraphPayload {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export type ChatRole = "user" | "assistant";

export interface ChatMessage {
  role: ChatRole;
  content: string;
}

/** Per-iteration agent activity, mirrors the SSE events from the loop. */
export interface AgentToolCall {
  toolCallId: string;
  tool: string;
  args: Record<string, unknown>;
  /** Set once the result arrives. null while in-flight. */
  result?: {
    nResults: number;
    nNew: number;
    nTotal: number;
    count: number | null;
    error: string | null;
  } | null;
}

export interface AgentStep {
  step: number;
  thought: string;
  calls: AgentToolCall[];
}

export interface AgentTrace {
  steps: AgentStep[];
  stepsUsed: number;
  finishReason: string | null;   // "model_stopped" | "finish_tool" | "max_steps"
  nCitations: number;
}

/** A single completed assistant turn the chat panel renders. */
export interface AssistantTurn {
  id: string;
  question: string;
  answer: string;
  plan: IntentPlan | null;
  citations: Citation[];
  status: "pending" | "streaming" | "done" | "error";
  error?: string;
  unknownCitations?: string[];
  usedCitations?: string[];
  /** Filled in incrementally as the agent loop runs. */
  trace: AgentTrace;
}
