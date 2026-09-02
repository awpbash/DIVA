"""Shared pydantic models for chat + retrieval.

Keep these flat and JSON-serializable — they cross the SSE boundary and the
React client mirrors them by hand.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


Intent = Literal[
    "factual",        # "what is the consumption charge rate?"
    "comparison",     # "compare X and Y" — synth must render a table
    "formula",        # "what's the default-interest formula?"
    "clause",         # "show me clause 9.3"
    "cross_ref",      # "what does 9.3 reference?"
    "aggregation",    # "how many sections mention force majeure?"
    "definition",     # "what does 'Unit' mean in this contract?"
    "other",
]


CitationKind = Literal[
    "evidence_span",
    "fact_mention",
    "reference",
    "block",
    "section_heading",
    "document_asset",     # a full-page diagram/schematic — page-level pointer
]


class IntentPlan(BaseModel):
    """Planner output. Drives retrieval shape + synth instructions."""
    intent: Intent
    key_terms: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    needs_table: bool = False
    rationale: str = ""


class Citation(BaseModel):
    """One retrievable citation atom.

    Most citations are EvidenceSpan nodes. Raw fragments that did not
    canonicalize are FactMention citations, and layout fallbacks can point at
    Block or section-heading atoms. evidence_id remains the public citation key
    for backwards-compatible [ev:...] tags.

    ``bbox`` is normalised 0..1 in PDF coordinate space — the frontend
    multiplies by rendered page size to draw the highlight rectangle.
    """
    evidence_id: str
    citation_kind: CitationKind = "evidence_span"
    canonicalized: Optional[bool] = None
    confidence_tier: Optional[str] = None   # canonical | reference | raw | layout
    doc_id: str
    page_no: int
    section_id: Optional[str] = None
    section_num: Optional[str] = None
    section_title: Optional[str] = None
    snippet: str
    bbox: Optional[list[float]] = None       # [x0, y0, x1, y1] normalised (envelope)
    # Per-page rects [{page_no, bbox}] — one per cited block/table-row.
    # Preferred over `bbox` by the viewer: a fact cited in two far-apart
    # blocks draws two tight rectangles, not one giant envelope.
    rects: Optional[list[dict]] = None
    fact_label: Optional[str] = None         # 'Obligation' | 'Rate' | ...
    fact_id: Optional[str] = None            # the fact's stable primary key
    fact_summary: Optional[str] = None       # one-line human-readable
    raw_label: Optional[str] = None          # harvest label for raw mentions
    row_context: Optional[str] = None        # full table-row text the snippet sits in
    # Graph-traversed context from derived fact-to-fact edges: conditions
    # (IF:), trigger events (TRIGGER:) and defined terms (TERM ...) linked
    # to this citation's fact. Newline-separated, prompt-ready.
    linked_context: Optional[str] = None
    score: float = 0.0
    # Access-policy tags (set at answer time by api/rag/policy.py). `sensitivity`
    # is the class this evidence belongs to (e.g. 'financial'); `restricted` is
    # true when the current viewer role may not see it — the UI blurs these.
    sensitivity: Optional[str] = None
    restricted: bool = False
    # Field-level verification consensus (set on knowledge-base OpsField
    # citations): who stands behind this value and how strongly. `verified_by`
    # holds verifier emails at retrieval time; chat.py swaps in display names
    # before the payload leaves the server.
    field_trust: Optional[str] = None          # human_validated | disputed | None
    verified_by: Optional[list[str]] = None
    verify_confidence: Optional[float] = None  # winning vote share, 0..1
    verify_votes: Optional[int] = None         # total standing votes
    # The bare field value (OpsField citations) — lets the citation forwarder
    # recognise when a raw-text citation evidences the same value as a
    # verified statement, and swap in the verified record.
    field_value: Optional[str] = None


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    doc_ids: Optional[list[str]] = None  # filter retrieval; None = all docs
    # Retrieval-eval switch: run plan + agent loop, emit the citation bundle,
    # then stop BEFORE synth. Lets the eval harness measure recall / routing
    # without paying for the streamed answer. Default False = normal chat.
    retrieval_only: bool = False
    # ADMIN-ONLY impersonation override ("answer as a default user would see
    # it" — debugging/evals). The effective role comes from the login session;
    # a non-admin's value here is ignored by the route.
    role: Optional[str] = None


class SubgraphRequest(BaseModel):
    evidence_ids: list[str]
    hops: int = 1


class GraphNode(BaseModel):
    id: str
    label: str
    title: str
    props: dict


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    type: str
    # Edge-borne explainability (match method / confidence / signals,
    # derivation provenance). Empty for plain structural edges. Surfaced in
    # the Explore graph's edge-inspector so an asserted cross-doc link can be
    # justified ("why does this mention RESOLVE_TO that canonical entity?").
    props: dict = Field(default_factory=dict)


class GraphPayload(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
