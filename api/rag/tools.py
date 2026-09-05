"""Retrieval tools exposed to the agent loop (Cosmos port).

Each tool is a small async function that takes typed kwargs and returns a
``list[Citation]``. The agent loop picks tools via OpenAI tool-calling.

Why a toolset instead of one vector query: the schema declares typed fact
labels with filterable properties, plus section- and block-level vector tiers.
A direct label lookup is exact and cheap; vector recall is the fallback when
intent is fuzzy. The agent chooses.

Every tool returns the same ``Citation`` shape so the loop can union them
into a single citation bundle keyed by ``evidence_id``.

Port shape (Neo4j → Cosmos document model, pipeline/store/model.py):
  * the old Cypher projections become: candidate selection (vector RAM /
    SQL filter / Python keyword score) + one shared Python enrichment step
    (``_enrich_evidence``) that joins sections, parent facts and the derived
    fact-to-fact edge items;
  * the confidentiality guards become plain field checks —
    ``evidence.cites_confidential`` / ``mention.cites_confidential`` /
    ``block.sensitivity`` are stamped at KM-build time (the cross-layer
    value net), so no per-query join can miss them;
  * Lucene/BM25 → candidate fetch via case-insensitive CONTAINS + Python
    ``keyword_score`` (pipeline/store/query.py) — deterministic and exact at
    this corpus size;
  * aggregations fetch the matching rows and do the arithmetic in Python
    (exact, emulator-proof).
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from functools import lru_cache
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

from pipeline.extraction.pack import load as load_pack
from pipeline.ontology import DEFAULT_DOCTYPE
from pipeline.store.aio import AsyncCosmosStore
from pipeline.store.query import keyword_score, tokenize

from .resolve import canonical_roles
from .schemas import Citation


# ---------------------------------------------------------------------------
# Shared row → Citation conversion
# ---------------------------------------------------------------------------


# Per-label retrieval metadata, read from the ACTIVE pack rather than written
# out here. The pack already declares a summary projection, an id key and a
# filterable allowlist for every fact type, and validates each against the
# label's declared properties. These were literal copies of one domain's
# answers, so on any other domain the labels they did not name lost their
# summary line, lost their stable id, and could not be filtered at all.


@lru_cache(maxsize=1)
def _fact_summary_keys() -> dict[str, tuple[str, ...]]:
    pack = load_pack()
    return {lab: tuple(pack.summary_keys(lab)) for lab in pack.fact_labels}


@lru_cache(maxsize=1)
def _id_key_by_label() -> dict[str, str]:
    pack = load_pack()
    return {lab: k for lab in pack.fact_labels if (k := pack.id_key(lab))}


# Column projections for the three EMBEDDING-BEARING kinds
# (pipeline/store/model.EMBEDDED_KINDS: evidence, section, block).
#
# Each of those rows carries a 3072-float vector, roughly 60KB serialised, and
# `SELECT *` drags it across the wire for fields nobody reads. Invisible on the
# local emulator, several megabytes per chat turn on live Azure.
#
# These lists are deliberately GENEROUS. A column named here that an item does
# not have simply comes back absent, which costs nothing. A column MISSING from
# here reads as None downstream, silently, which is the failure this codebase
# keeps having. When in doubt, add the column.
_EVIDENCE_COLS = (
    "id", "doc_id", "kind", "page_no", "section_id", "section_num",
    "section_title", "target_section_id", "agreement_id", "fact_id",
    "text_span", "snippet", "raw_label", "row_context", "rects_json",
    "bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1",
    "canonicalized", "confidence", "confidence_tier", "evidence_type",
    "cites_confidential", "sensitivity", "via",
    "linked_conditions", "linked_events", "linked_terms",
    "linked_computes", "linked_measures",
    # Present on stored evidence and read by the citation path. Verified
    # against a live row rather than assumed: a column left out here is a
    # silent None downstream, which is the whole failure mode.
    "evidence_id", "block_ids", "raw_fact_id", "snippet_ok",
)
_SECTION_COLS = (
    "id", "doc_id", "kind", "section_id", "section_num", "title", "text",
    "page_start", "page_no", "bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1",
    "sensitivity",
)
_BLOCK_COLS = (
    "id", "doc_id", "kind", "block_id", "section_id", "page_no", "text",
    "bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1", "sensitivity", "kind_hint",
)

# Cosmos rejects these as a bare property path AND as a column alias, so a
# projection cannot carry them under their own name. Nothing in this module
# reads them off a section or block row, so they are simply left out rather
# than aliased to something the consumers would then have to know about.
# `order` is the one that matters: it is why the section projection has no
# reading-order column. Same family as the `AS value` trap covered by tests.
_UNPROJECTABLE = {"order", "value", "group", "key", "end"}


def _cols(names: tuple[str, ...]) -> str:
    """`c.a, c.b, …` for a SELECT list, minus anything Cosmos cannot alias."""
    return ", ".join(f"c.{n}" for n in names if n not in _UNPROJECTABLE)


def _bbox(r: dict) -> list[float] | None:
    parts = [r.get("bbox_x0"), r.get("bbox_y0"), r.get("bbox_x1"), r.get("bbox_y1")]
    if any(p is None for p in parts):
        return None
    return [float(p) for p in parts]


def _fact_summary(label: str | None, props: dict | None) -> str | None:
    if not label or not props:
        return None
    keys = _fact_summary_keys().get(label)
    if not keys:
        return None
    parts: list[str] = []
    for k in keys:
        v = props.get(k)
        if v is None or v == "":
            continue
        parts.append(str(v))
    return " · ".join(parts) if parts else None


def _fact_id(label: str | None, props: dict | None) -> str | None:
    if not label or not props:
        return None
    key = _id_key_by_label().get(label)
    return props.get(key) if key else None


def _parse_rects(rects_json) -> list[dict] | None:
    """Per-page rects from the citation atom's JSON property; None when
    absent or unparseable (frontend falls back to the envelope bbox)."""
    if not rects_json:
        return None
    try:
        rects = json.loads(rects_json)
    except (json.JSONDecodeError, TypeError):
        return None
    return rects if isinstance(rects, list) and rects else None


def _infer_citation_kind(r: dict) -> str:
    """Best-effort citation kind for projections that don't set it."""
    kind = r.get("citation_kind")
    if kind:
        return str(kind)
    eid = str(r.get("evidence_id") or "")
    if ":m:" in eid:
        return "fact_mention"
    if ":block:" in eid:
        return "block"
    if eid.endswith(":heading"):
        return "section_heading"
    if r.get("evidence_type") == "inferred_reference":
        return "reference"
    return "evidence_span"


def _default_confidence_tier(kind: str) -> str:
    return {
        "evidence_span": "canonical",
        "reference": "reference",
        "fact_mention": "raw",
        "block": "layout",
        "section_heading": "layout",
    }.get(kind, "canonical")


def _linked_context(r: dict) -> str | None:
    """Flatten the linked_* collections into the prompt-ready block:
    one line per linked fact, prefixed by its relationship kind."""
    parts: list[str] = []
    parts += [f"IF: {t}" for t in (r.get("linked_conditions") or []) if t]
    parts += [f"TRIGGER: {t}" for t in (r.get("linked_events") or []) if t]
    parts += [f"TERM {t}" for t in (r.get("linked_terms") or []) if t]
    parts += [f"COMPUTES: {t}" for t in (r.get("linked_computes") or []) if t]
    parts += [f"MEASURES: {t}" for t in (r.get("linked_measures") or []) if t]
    return "\n".join(parts) if parts else None


# status → the tag the synth model reads. 'restated' is a value a LATER document
# re-states verbatim: still the value in force, so it must read as CURRENT (never
# "superseded by itself" — the 0.0241-superseded-0.0241 display bug).
_CURRENCY_TAGS = {
    "active": "CURRENT",
    "restated": "CURRENT (re-stated later; value unchanged)",
    "superseded": "superseded",
}


def _currency_prefixed(fact_props: dict | None, summary: str | None) -> str | None:
    """Prefix a summary with its supersedence currency so stale data is
    visible: superseded statements are tagged and synth prefers the active
    one. The tag is only present when the KM layer materialised it."""
    if not fact_props:
        return summary
    status, eff = fact_props.get("status"), fact_props.get("effective_date")
    tag = _CURRENCY_TAGS.get(status or "")
    if not tag or not eff:
        return summary
    return f"[{tag} · effective {eff}] " + (summary or "")


def _review_consensus(props: dict | None) -> dict:
    """Reviewer-confidence fields, read off whatever node carries the review
    stamps (an OpsField row, or a KM fact node: ``_propagate_to_km`` in
    review.py writes the same keys onto both: ``trust``/``verified``,
    ``confidence``, ``verifiers``, ``n_votes``, ``disputed``). One derivation
    so a citation shows the same "verified by N of M" badge everywhere it is
    backed by a reviewed fact, not only on the structured field-lookup tools
    that happened to read it first."""
    props = props or {}
    if props.get("trust") == "human_validated" or props.get("verified"):
        field_trust = "human_validated"
    elif props.get("disputed"):
        field_trust = "disputed"
    else:
        field_trust = None
    return {
        "field_trust": field_trust,
        "verified_by": (list(props.get("verifiers") or []) or None),
        "verify_confidence": (float(props["confidence"])
                              if props.get("confidence") is not None else None),
        "verify_votes": (int(props["n_votes"])
                         if props.get("n_votes") is not None else None),
    }


def _row_to_citation(r: dict, *, default_score: float = 0.0) -> Citation:
    fact_labels = r.get("fact_labels") or []
    fact_label = next(
        (lab for lab in fact_labels if lab in _id_key_by_label()),
        fact_labels[0] if fact_labels else None,
    )
    fact_props = dict(r["fact_props"]) if r.get("fact_props") else None
    citation_kind = _infer_citation_kind(r)
    return Citation(
        evidence_id=r["evidence_id"],
        citation_kind=citation_kind,
        canonicalized=(
            bool(r["canonicalized"]) if r.get("canonicalized") is not None
            else (False if citation_kind == "fact_mention" else None)
        ),
        confidence_tier=(
            r.get("confidence_tier") or _default_confidence_tier(citation_kind)
        ),
        doc_id=r["doc_id"],
        page_no=int(r.get("page_no") or 0),
        section_id=r.get("section_id"),
        section_num=r.get("section_num"),
        section_title=r.get("section_title"),
        snippet=r.get("snippet") or "",
        bbox=_bbox(r),
        rects=_parse_rects(r.get("rects_json")),
        fact_label=fact_label,
        fact_id=_fact_id(fact_label, fact_props),
        fact_summary=_currency_prefixed(fact_props, _fact_summary(fact_label, fact_props)),
        raw_label=r.get("raw_label"),
        row_context=r.get("row_context"),
        linked_context=_linked_context(r),
        score=float(r.get("score") if r.get("score") is not None else default_score),
        **_review_consensus(fact_props),
    )


def _verification_boost(c: Citation) -> float:
    """A nudge, not a gate: unreviewed evidence must stay fully citable (most
    of the corpus has no review yet, and a relevant unreviewed clause beats an
    irrelevant reviewed one), so this only tips close calls rather than
    overriding topical relevance. More reviewers moves it further, capped so
    a single vote isn't indistinguishable from a five-person consensus.
    Disputed evidence (verifiers actively disagree) gets the opposite nudge."""
    if c.field_trust == "human_validated":
        return 0.05 + 0.01 * min(c.verify_votes or 1, 5)
    if c.field_trust == "disputed":
        return -0.05
    return 0.0


def _rank_citations(citations: list[Citation], k: int | None = None) -> list[Citation]:
    """Dedupe by citation id, then rank by relevance with a verification
    nudge, then prefer stronger tiers at equal score. A `k` cutoff drops the
    lowest-priority items, so the nudge is what lets a well-reviewed clause
    survive a truncation an equally-relevant unreviewed one wouldn't."""
    tier_rank = {"canonical": 0, "reference": 1, "raw": 2, "layout": 3}
    by_id: dict[str, Citation] = {}
    for c in citations:
        if not c.evidence_id:
            continue
        existing = by_id.get(c.evidence_id)
        if existing is None or (
            c.score + _verification_boost(c),
            -tier_rank.get(c.confidence_tier or "", 9),
        ) > (
            existing.score + _verification_boost(existing),
            -tier_rank.get(existing.confidence_tier or "", 9),
        ):
            by_id[c.evidence_id] = c
    out = sorted(
        by_id.values(),
        key=lambda c: (
            -(c.score + _verification_boost(c)),
            tier_rank.get(c.confidence_tier or "", 9),
            c.page_no,
            c.evidence_id,
        ),
    )
    return out[:k] if k is not None else out


# ---------------------------------------------------------------------------
# Store fetch helpers (the shared enrichment that replaced the projections)
# ---------------------------------------------------------------------------


# Roles cleared for confidential text (mirror of the OpsField source filter).
_CLEARED_TEXT_ROLES = {"admin", "confidential", "finance"}


def _text_cleared(role: str | None) -> bool:
    return (role or "").lower() in _CLEARED_TEXT_ROLES


def _span_visible(item: dict, role: str | None) -> bool:
    """Evidence/mention-level confidentiality: a span citing a confidential
    block is invisible to uncleared roles. ``cites_confidential`` is stamped
    at KM-build time (the cross-layer value net)."""
    return _text_cleared(role) or not item.get("cites_confidential")


def _block_visible(item: dict, role: str | None) -> bool:
    return _text_cleared(role) or (item.get("sensitivity") or "general") != "confidential"


# Kinds that carry an embedding, and the columns to project instead of `*`.
# Fetching one of these without a projection pulls a 3072-float vector per row.
_EMBEDDED_COLS = {
    "evidence": _EVIDENCE_COLS,
    "section": _SECTION_COLS,
    "block": _BLOCK_COLS,
}


async def _fetch_by_ids(store: AsyncCosmosStore, ids: list[str],
                        kind: str | None = None) -> dict[str, dict]:
    """id -> item for a small id list (cross-partition IN fetch).

    Projects the embedding-bearing kinds. Three of the call sites below fetch
    evidence, section and block by id, so the default `SELECT *` was pulling a
    vector per row purely to read a snippet and a bounding box.
    """
    if not ids:
        return {}
    cols = _cols(_EMBEDDED_COLS[kind]) if kind in _EMBEDDED_COLS else "*"
    sql = f"SELECT {cols} FROM c WHERE ARRAY_CONTAINS(@ids, c.id)"
    params = [{"name": "@ids", "value": sorted(set(ids))}]
    if kind:
        sql += " AND c.kind = @kind"
        params.append({"name": "@kind", "value": kind})
    return {r["id"]: r for r in await store.query(sql, params)}


_VIA_RANK = {"same_block": 0, "cross_ref": 1, "same_section": 2}


async def _linked_context_fields(store: AsyncCosmosStore,
                                 fact_ids: list[str]) -> dict[str, dict]:
    """fact_id -> {linked_conditions, linked_events, linked_terms,
    linked_computes, linked_measures}. Walks the derived fact-to-fact edge
    items so a retrieved fact arrives WITH its condition / trigger /
    defined-term context — fetched from the store, not re-inferred by the
    LLM from whatever other chunks happened to make the retrieval cut."""
    if not fact_ids:
        return {}
    edges = await store.query(
        "SELECT c.src, c.tgt, c.rel, c.via FROM c WHERE c.kind = 'edge' "
        "AND ARRAY_CONTAINS(@srcs, c.src) AND ARRAY_CONTAINS(@rels, c.rel)",
        [{"name": "@srcs", "value": sorted(set(fact_ids))},
         {"name": "@rels", "value": ["CONDITIONED_ON", "TRIGGERED_BY",
                                     "USES_TERM", "COMPUTES", "MEASURES"]}])
    if not edges:
        return {}
    targets = await _fetch_by_ids(store, [e["tgt"] for e in edges], kind="fact")

    by_fact: dict[str, dict[str, list[tuple[int, str]]]] = defaultdict(
        lambda: defaultdict(list))
    for e in edges:
        t = targets.get(e["tgt"])
        if not t:
            continue
        rank = _VIA_RANK.get(e.get("via") or "", 3)
        rel = e["rel"]
        if rel == "CONDITIONED_ON":
            txt = str(t.get("condition_text") or "")[:220]
        elif rel == "TRIGGERED_BY":
            txt = str(t.get("trigger_text") or t.get("description")
                      or t.get("name") or "")[:180]
        elif rel == "USES_TERM":
            txt = f"{t.get('term')} = " + str(t.get("definition") or "")[:200]
        elif rel == "COMPUTES":
            txt = str(t.get("name") or t.get("charge_type")
                      or t.get("rate_type") or "")[:140]
        else:  # MEASURES
            txt = str(t.get("name") or t.get("equipment_type") or "")[:120]
        if txt.strip():
            by_fact[e["src"]][rel].append((rank, txt))

    caps = {"CONDITIONED_ON": 3, "TRIGGERED_BY": 2, "USES_TERM": 4,
            "COMPUTES": 2, "MEASURES": 2}
    keys = {"CONDITIONED_ON": "linked_conditions", "TRIGGERED_BY": "linked_events",
            "USES_TERM": "linked_terms", "COMPUTES": "linked_computes",
            "MEASURES": "linked_measures"}
    out: dict[str, dict] = {}
    for fid, rels in by_fact.items():
        fields: dict[str, list[str]] = {}
        for rel, pairs in rels.items():
            pairs.sort(key=lambda p: p[0])
            seen: list[str] = []
            for _, txt in pairs:
                if txt not in seen:
                    seen.append(txt)
                if len(seen) >= caps[rel]:
                    break
            fields[keys[rel]] = seen
        out[fid] = fields
    return out


async def _enrich_evidence(store: AsyncCosmosStore, evs: list[dict],
                           scores: dict[str, float] | None = None,
                           *, with_links: bool = True) -> list[dict]:
    """Evidence items → projection rows compatible with ``_row_to_citation``
    (section context + parent fact + linked context) — the Python equivalent
    of the old shared Cypher projection."""
    if not evs:
        return []
    sections = await _fetch_by_ids(
        store, [e["section_id"] for e in evs if e.get("section_id")], kind="section")
    facts = await _fetch_by_ids(
        store, [e["fact_id"] for e in evs if e.get("fact_id")], kind="fact")
    links = (await _linked_context_fields(store, list(facts.keys()))
             if with_links else {})
    rows: list[dict] = []
    for e in evs:
        s = sections.get(e.get("section_id")) or {}
        n = facts.get(e.get("fact_id"))
        row = {
            "evidence_id": e["id"],
            "citation_kind": ("reference" if e.get("evidence_type") == "inferred_reference"
                              else "evidence_span"),
            "canonicalized": True,
            "confidence_tier": ("reference" if e.get("evidence_type") == "inferred_reference"
                                else "canonical"),
            "evidence_type": e.get("evidence_type"),
            "raw_label": None,
            "doc_id": e.get("doc_id"),
            "page_no": e.get("page_no"),
            "snippet": e.get("text_span"),
            "bbox_x0": e.get("bbox_x0"), "bbox_y0": e.get("bbox_y0"),
            "bbox_x1": e.get("bbox_x1"), "bbox_y1": e.get("bbox_y1"),
            "section_id": s.get("id"),
            "section_num": s.get("section_num"),
            "section_title": s.get("title"),
            "row_context": e.get("row_context"),
            "rects_json": e.get("rects_json"),
            "fact_labels": (n or {}).get("labels") or [],
            "fact_props": n,
            "score": (scores or {}).get(e["id"]),
        }
        row.update(links.get(e.get("fact_id") or "", {}))
        rows.append(row)
    return rows


def _block_row(b: dict, s: dict | None, score: float) -> dict:
    """Block item → projection row (evidence_id is the synthetic block id the
    /evidence endpoint resolves)."""
    return {
        "evidence_id": f"{b['doc_id']}:block:{b['id']}",
        "doc_id": b.get("doc_id"),
        "page_no": b.get("page_no"),
        "snippet": b.get("text"),
        "bbox_x0": b.get("bbox_x0"), "bbox_y0": b.get("bbox_y0"),
        "bbox_x1": b.get("bbox_x1"), "bbox_y1": b.get("bbox_y1"),
        "section_id": (s or {}).get("id"),
        "section_num": (s or {}).get("section_num"),
        "section_title": (s or {}).get("title"),
        "fact_labels": [],
        "fact_props": None,
        "score": score,
    }


async def _sections_of_blocks(store: AsyncCosmosStore,
                              blocks: list[dict]) -> dict[str, dict]:
    return await _fetch_by_ids(
        store, [b["section_id"] for b in blocks if b.get("section_id")],
        kind="section")


def _contains_any_clause(field: str, tokens: list[str], params: list[dict],
                         prefix: str) -> str:
    """Case-insensitive CONTAINS over any token — the candidate fetch that
    replaced the Lucene index (final ranking is Python keyword_score)."""
    ors = []
    for i, t in enumerate(tokens):
        pname = f"@{prefix}{i}"
        params.append({"name": pname, "value": t})
        ors.append(f"CONTAINS(c.{field}, {pname}, true)")
    return "(" + " OR ".join(ors) + ")" if ors else "false"


def _scope_sql(doc_ids: list[str] | None, params: list[dict]) -> str:
    if not doc_ids:
        return "true"
    params.append({"name": "@scope_ids", "value": list(doc_ids)})
    return "ARRAY_CONTAINS(@scope_ids, c.doc_id)"


# ---------------------------------------------------------------------------
# Embedding helper
# ---------------------------------------------------------------------------


async def _embed(client: AsyncOpenAI, model: str, text: str) -> list[float]:
    resp = await client.embeddings.create(model=model, input=[text])
    return resp.data[0].embedding


# ---------------------------------------------------------------------------
# Tool 1 — Vector search over EvidenceSpans
# ---------------------------------------------------------------------------


async def vector_search_evidence(
    *, store: AsyncCosmosStore, client: AsyncOpenAI, embed_model: str,
    query: str, k: int = 6, doc_ids: list[str] | None = None,
    min_score: float = 0.55, include_raw_mentions: bool = True,
    role: str | None = None,
) -> list[Citation]:
    """Citation-atom recall.

    Canonical evidence spans are recalled semantically through the vector
    index. Raw orphan mentions are added by keyword with a low confidence
    tier so they can rescue fine-grained fragments without outranking typed
    facts."""
    vec = await _embed(client, embed_model, query)
    # Over-fetch so the confidentiality filter can't starve the result.
    hits = await store.vector_search("evidence", vec, max(k * 2, k + 4), doc_ids)
    scores = {i: s for i, s in hits}
    items = await _fetch_by_ids(store, list(scores), kind="evidence")
    visible = [it for it in items.values() if _span_visible(it, role)]
    rows = await _enrich_evidence(store, visible, scores)
    cits = [_row_to_citation(r) for r in rows]
    canonical_hits = [c for c in cits if c.score >= min_score]
    raw_hits: list[Citation] = []
    if include_raw_mentions:
        raw_hits = await find_orphan_fragments(
            store=store, query=query, doc_ids=doc_ids, k=min(max(k, 1), 8),
            role=role,
        )
    return _rank_citations(canonical_hits + raw_hits, k)


# ---------------------------------------------------------------------------
# Tool 2 — Vector search over Sections
# ---------------------------------------------------------------------------


async def vector_search_sections(
    *, store: AsyncCosmosStore, client: AsyncOpenAI, embed_model: str,
    query: str, k: int = 3, doc_ids: list[str] | None = None,
    min_score: float = 0.55, role: str | None = None,
) -> list[Citation]:
    """Section-level semantic recall. Good for vague navigational questions
    like 'what does Schedule 1B cover?' where the answer is a whole section,
    not a specific fact. Each hit projects through the section's first
    visible evidence span (so the frontend has a bbox); a section with no
    spans synthesises a citation from the heading anchor."""
    vec = await _embed(client, embed_model, query)
    hits = await store.vector_search("section", vec, k, doc_ids)
    if not hits:
        return []
    sections = await _fetch_by_ids(store, [i for i, _ in hits], kind="section")
    out: list[Citation] = []
    for sid, score in hits:
        s = sections.get(sid)
        if s is None or score < min_score:
            continue
        evs = await store.query(
            f"SELECT {_cols(_EVIDENCE_COLS)} FROM c "
            "WHERE c.kind = 'evidence' AND c.section_id = @sid",
            [{"name": "@sid", "value": sid}], pk=s["doc_id"])
        evs = [e for e in evs if _span_visible(e, role)]
        evs.sort(key=lambda e: e.get("page_no") or 0)
        e = evs[0] if evs else {}
        fact = None
        if e.get("fact_id"):
            facts = await _fetch_by_ids(store, [e["fact_id"]], kind="fact")
            fact = facts.get(e["fact_id"])
        out.append(_row_to_citation({
            "evidence_id": e.get("id") or f"{sid}:heading",
            "doc_id": s["doc_id"],
            "page_no": e.get("page_no") or s.get("page_start"),
            "snippet": e.get("text_span") or s.get("title"),
            "bbox_x0": e.get("bbox_x0") if e else s.get("bbox_x0"),
            "bbox_y0": e.get("bbox_y0") if e else s.get("bbox_y0"),
            "bbox_x1": e.get("bbox_x1") if e else s.get("bbox_x1"),
            "bbox_y1": e.get("bbox_y1") if e else s.get("bbox_y1"),
            "section_id": sid,
            "section_num": s.get("section_num"),
            "section_title": s.get("title"),
            "fact_labels": (fact or {}).get("labels") or [],
            "fact_props": None,
            "score": score,
        }))
    return out


# ---------------------------------------------------------------------------
# Tool 2b — Vector search over Blocks
# ---------------------------------------------------------------------------


async def vector_search_blocks(
    *, store: AsyncCosmosStore, client: AsyncOpenAI, embed_model: str,
    query: str, k: int = 5, doc_ids: list[str] | None = None,
    min_score: float = 0.55, role: str | None = None,
) -> list[Citation]:
    """Block-level semantic recall. Use this when neither
    `vector_search_evidence` nor `vector_search_sections` hits, e.g. for
    figure / diagram / chart text the normaliser didn't carve into a
    typed fact. Returns the raw block text as the snippet.

    Raw text carries no fact label the answer policy could key on, so blocks
    tagged confidential at KM-build time are filtered HERE, at the source,
    for uncleared roles — the leak the label policy can't see."""
    vec = await _embed(client, embed_model, query)
    hits = await store.vector_search("block", vec, max(k * 2, k + 4), doc_ids)
    scores = {i: s for i, s in hits}
    blocks = await _fetch_by_ids(store, list(scores), kind="block")
    visible = [b for b in blocks.values() if _block_visible(b, role)]
    sections = await _sections_of_blocks(store, visible)
    rows = [_block_row(b, sections.get(b.get("section_id")), scores[b["id"]])
            for b in visible]
    rows.sort(key=lambda r: -(r["score"] or 0.0))
    cits = [_row_to_citation(r) for r in rows[:k]]
    return [c for c in cits if c.score >= min_score]


# ---------------------------------------------------------------------------
# Corpus catalog — injected into agent + synth prompts every chat
# ---------------------------------------------------------------------------


async def _catalog_rows(store: AsyncCosmosStore,
                        doc_ids: list[str] | None) -> list[dict]:
    params: list[dict] = []
    docs = await store.query(
        f"SELECT * FROM c WHERE c.kind = 'document' AND {_scope_sql(doc_ids, params)}",
        params)
    params = []
    agrs = {a["doc_id"]: a for a in await store.query(
        f"SELECT * FROM c WHERE c.kind = 'agreement' AND {_scope_sql(doc_ids, params)}",
        params)}
    params = []
    parties = await store.query(
        "SELECT c.doc_id, c.role, c['name'] AS name FROM c WHERE c.kind = 'fact' "
        f"AND c.label = 'Party' AND ARRAY_CONTAINS(@roles, c.role) "
        f"AND {_scope_sql(doc_ids, params)}",
        [{"name": "@roles", "value": list(canonical_roles())}] + params)
    parties_by_doc: dict[str, list[dict]] = defaultdict(list)
    for p in parties:
        entry = {"role": p.get("role"), "name": p.get("name")}
        if entry not in parties_by_doc[p["doc_id"]]:
            parties_by_doc[p["doc_id"]].append(entry)
    rows = []
    for d in docs:
        a = agrs.get(d["doc_id"]) or {}
        rows.append({
            "doc_id": d["doc_id"],
            "title": d.get("title") or a.get("title"),
            "group": d.get("group"),
            "doctype": d.get("doctype"),
            "total_pages": d.get("total_pages"),
            "document_date": a.get("document_date"),
            "parties": parties_by_doc.get(d["doc_id"], []),
        })
    rows.sort(key=lambda r: (r.get("group") or "", r.get("document_date") or "9999"))
    return rows


async def build_catalog_text(
    store: AsyncCosmosStore, doc_ids: list[str] | None = None,
) -> str:
    """One line per document in the KB. Injected into the agent system
    prompt (so it knows what exists and can fan out per doc) and the synth
    prompt (so answers can name documents instead of citing bare ids).
    Kept compact — at corpus sizes where this gets big, swap to a tool."""
    rows = await _catalog_rows(store, doc_ids)
    lines = []
    for r in rows:
        parts = [f"doc_id={r['doc_id']}"]
        if r.get("title"):
            parts.append(f'"{r["title"]}"')
        if r.get("group"):
            parts.append(f"family: {r['group']}")
        if r.get("doctype"):
            parts.append(str(r["doctype"]))
        parts.append(f"date: {r.get('document_date') or 'unknown'}")
        if r.get("total_pages"):
            parts.append(f"{r['total_pages']} pages")
        for p in sorted(r.get("parties") or [], key=lambda x: x.get("role") or ""):
            if p.get("name"):
                parts.append(f"{p['role']}: {p['name']}")
        lines.append("- " + " | ".join(parts))
    return "\n".join(lines) if lines else "(no documents loaded)"


# ---------------------------------------------------------------------------
# Document identity — title map + scoped-document block (provenance + links)
# ---------------------------------------------------------------------------


async def _doc_rows(store: AsyncCosmosStore, doc_ids: list[str] | None) -> list[dict]:
    params: list[dict] = []
    docs = await store.query(
        f"SELECT * FROM c WHERE c.kind = 'document' AND {_scope_sql(doc_ids, params)}",
        params)
    params = []
    agrs = {a["doc_id"]: a for a in await store.query(
        f"SELECT * FROM c WHERE c.kind = 'agreement' AND {_scope_sql(doc_ids, params)}",
        params)}
    return [{
        "doc_id": d["doc_id"],
        "title": (d.get("title") or (agrs.get(d["doc_id"]) or {}).get("title")
                  or d.get("doctype") or d["doc_id"]),
        "grp": d.get("group"),
        "doctype": d.get("doctype"),
        "document_date": (agrs.get(d["doc_id"]) or {}).get("document_date"),
        "total_pages": d.get("total_pages"),
    } for d in docs]


async def build_doc_titles(
    store: AsyncCosmosStore, doc_ids: list[str] | None = None,
) -> dict[str, str]:
    """doc_id -> human title. Used to stamp document identity onto every
    citation the agent and synth see, so neither is blind to WHICH contract a
    snippet came from. Pass doc_ids=None for the whole corpus."""
    return {r["doc_id"]: r["title"] for r in await _doc_rows(store, doc_ids)}


async def build_scope_docs_block(
    store: AsyncCosmosStore, doc_ids: list[str] | None,
) -> str:
    """Render the resolved contract family as a citable document list with
    PDF links — the source for 'show me the documents for X' (doc-links) and
    the named scope the answer should stay within. Empty when unscoped."""
    if not doc_ids:
        return ""
    rows = await _doc_rows(store, doc_ids)
    if not rows:
        return ""
    rows.sort(key=lambda r: (r.get("grp") or "", r.get("title") or ""))
    lines = []
    for r in rows:
        bits = [f'"{r.get("title") or r["doc_id"]}"']
        if r.get("doctype"):
            bits.append(str(r["doctype"]))
        if r.get("total_pages"):
            bits.append(f"{r['total_pages']}p")
        if r.get("grp"):
            bits.append(f"family: {r['grp']}")
        bits.append(f"link: /pdf/{r['doc_id']}")
        lines.append("- " + " | ".join(bits))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 3 — Exact / prefix section lookup
# ---------------------------------------------------------------------------


async def lookup_section(
    *, store: AsyncCosmosStore, section_num: str,
    doc_ids: list[str] | None = None, k: int = 20,
    role: str | None = None,
) -> list[Citation]:
    """Exact / prefix lookup for 'show me clause 9.3'. Returns every
    evidence span in the matched section(s) plus child sub-sections.
    The prefix match handles '9' returning '9', '9.1', '9.1(a)', ...
    Confidential spans are filtered at the source for uncleared roles
    (same model as the vector tools)."""
    num = (section_num or "").strip()
    params: list[dict] = [
        {"name": "@num", "value": num},
        {"name": "@dot", "value": num + "."},
        {"name": "@par", "value": num + "("},
    ]
    sections = await store.query(
        f"SELECT {_cols(_SECTION_COLS)} FROM c WHERE c.kind = 'section' AND "
        "(c.section_num = @num OR STARTSWITH(c.section_num, @dot) "
        "OR STARTSWITH(c.section_num, @par)) AND "
        + _scope_sql(doc_ids, params), params)
    sections.sort(key=lambda s: s.get("section_num") or "")
    rows: list[dict] = []
    for s in sections:
        evs = await store.query(
            f"SELECT {_cols(_EVIDENCE_COLS)} FROM c "
            "WHERE c.kind = 'evidence' AND c.section_id = @sid",
            [{"name": "@sid", "value": s["id"]}], pk=s["doc_id"])
        evs = [e for e in evs if _span_visible(e, role)]
        evs.sort(key=lambda e: e.get("page_no") or 0)
        if not evs:
            rows.append({
                "evidence_id": f"{s['id']}:heading", "doc_id": s["doc_id"],
                "page_no": s.get("page_start"), "snippet": s.get("title"),
                "bbox_x0": s.get("bbox_x0"), "bbox_y0": s.get("bbox_y0"),
                "bbox_x1": s.get("bbox_x1"), "bbox_y1": s.get("bbox_y1"),
                "section_id": s["id"], "section_num": s.get("section_num"),
                "section_title": s.get("title"),
                "fact_labels": [], "fact_props": None, "score": 0.95,
            })
            continue
        enriched = await _enrich_evidence(
            store, evs, {e["id"]: 0.95 for e in evs}, with_links=False)
        rows.extend(enriched)
        if len(rows) >= k:
            break
    return [_row_to_citation(r, default_score=0.95) for r in rows[:k]]


# ---------------------------------------------------------------------------
# Tool 4 — Defined-term lookup
# ---------------------------------------------------------------------------


async def lookup_defined_term(
    *, store: AsyncCosmosStore, term: str,
    doc_ids: list[str] | None = None, k: int = 5,
    role: str | None = None,
) -> list[Citation]:
    """Exact + CONTAINS match on DefinedTerm facts. Returns document-scoped
    definition evidence spans. Falls back to a phrase search for the
    definition clause in raw paragraph text ('"X" means…') when no
    DefinedTerm matches. The fact query itself is doc-scoped, so sibling
    documents' identical terms can never crowd the in-scope definition out
    of the top-k slots. Confidential spans are filtered at the source for
    uncleared roles. (CanonicalTerm peer expansion is dormant — the corpus
    mints no CanonicalTerm hubs yet.)"""
    needle = " ".join((term or "").lower().split())
    if not needle:
        return []
    fact_params: list[dict] = [{"name": "@n", "value": needle}]
    fact_scope = _scope_sql(doc_ids, fact_params)
    hits = await store.query(
        "SELECT * FROM c WHERE c.kind = 'fact' AND c.label = 'DefinedTerm' AND "
        "(c.normalized_term = @n OR CONTAINS(c.normalized_term, @n) "
        f"OR CONTAINS(LOWER(c.term), @n)) AND {fact_scope}",
        fact_params)
    hits.sort(key=lambda t: (0 if t.get("normalized_term") == needle else 1,
                             len(t.get("normalized_term") or t.get("term") or "")))
    hits = hits[:k]
    if hits:
        rows: list[dict] = []
        for t in hits:
            evs = await store.query(
                f"SELECT {_cols(_EVIDENCE_COLS)} FROM c "
                "WHERE c.kind = 'evidence' AND c.fact_id = @fid",
                [{"name": "@fid", "value": t["id"]}], pk=t["doc_id"])
            for e in evs:
                if not _span_visible(e, role):
                    continue
                if not e.get("text_span"):
                    e = {**e, "text_span": t.get("definition")}
                rows.append(e)
        enriched = await _enrich_evidence(
            store, rows, {e["id"]: 0.9 for e in rows}, with_links=False)
        out = [_row_to_citation(r, default_score=0.9) for r in enriched]
        if out:
            return out

    # Fallback: the definition CLAUSE itself in raw paragraph text.
    base = re.sub(r'["\\\\]', " ", term or "").strip()
    if not base:
        return []
    params: list[dict] = [
        {"name": "@p0", "value": f"{base} means"},
        {"name": "@p1", "value": f"{base} shall mean"},
        {"name": "@p2", "value": f"{base} has the meaning"},
    ]
    scope_clause = _scope_sql(doc_ids, params)
    blocks = await store.query(
        f"SELECT {_cols(_BLOCK_COLS)} FROM c WHERE c.kind = 'block' AND "
        "(CONTAINS(c.text, @p0, true) OR CONTAINS(c.text, @p1, true) "
        f"OR CONTAINS(c.text, @p2, true)) AND {scope_clause}", params)
    blocks = [b for b in blocks if _block_visible(b, role)][:k]
    sections = await _sections_of_blocks(store, blocks)
    return [_row_to_citation(
        _block_row(b, sections.get(b.get("section_id")), 0.85),
        default_score=0.85) for b in blocks]


# ---------------------------------------------------------------------------
# Human-correction net over the legacy fact tier
# ---------------------------------------------------------------------------
#
# A verified human correction rewrites the OpsField, but the legacy fact tier
# (typed facts + their evidence spans) still carries the ORIGINAL machine
# value. These helpers fetch the corrected fields' displaced value strings per
# document so the fact-serving tools can TAG matching facts (the audit trail
# stays visible) and aggregation can EXCLUDE them from arithmetic. Matching is
# conservative and deterministic: same document only, normalized string
# equality/containment either way, or identical parsed-number sets. No fuzzy
# scoring.


def _norm_value_text(s) -> str:
    return " ".join(str(s or "").lower().split())


def _matches_displaced(displaced: str, *texts) -> bool:
    """Does a displaced machine value confidently match any of the fact's
    rendered texts (summary / snippet)? Pure, unit-tested."""
    from pipeline.kb.km import parse_numbers
    d = _norm_value_text(displaced)
    if not d:
        return False
    d_nums = set(parse_numbers(displaced))
    for t in texts:
        h = _norm_value_text(t)
        if not h:
            continue
        if d in h or h in d:
            return True
        if d_nums and d_nums == set(parse_numbers(t)):
            return True
    return False


async def _corrected_fields_by_doc(
    store: AsyncCosmosStore, doc_ids: list[str],
) -> dict[str, list[dict]]:
    """doc_id -> [{title, value, displaced}] for verified human corrections
    (opsfields whose winning value displaced machine value strings). Callers
    pass the concrete docs of their result set, so the fetch stays small."""
    if not doc_ids:
        return {}
    params: list[dict] = []
    rows = await store.query(
        "SELECT c.doc_id, c.title, c['value'] AS field_value, "
        "c.displaced_values FROM c WHERE c.kind = 'opsfield' "
        "AND c.verified = true AND IS_DEFINED(c.displaced_values) "
        f"AND {_scope_sql(doc_ids, params)}", params)
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        displaced = [str(v) for v in (r.get("displaced_values") or [])
                     if str(v).strip()]
        if not displaced:
            continue
        out[r["doc_id"]].append({
            "title": r.get("title"),
            "value": r.get("field_value"),
            "displaced": displaced,
        })
    return dict(out)


def _stamp_corrected_citations(citations: list[Citation],
                               corrected: dict[str, list[dict]]) -> None:
    """Append a visible marker to fact-backed citations whose value a verified
    human correction displaced. Tag, never drop: the stale machine value
    stays auditable but can no longer read as the value in force."""
    if not corrected:
        return
    for c in citations:
        if not c.fact_label:
            continue
        for cf in corrected.get(c.doc_id, ()):
            if any(_matches_displaced(d, c.fact_summary, c.snippet)
                   for d in cf["displaced"]):
                marker = f"[superseded by human correction: {cf['value']}]"
                if marker not in (c.fact_summary or ""):
                    c.fact_summary = (f"{c.fact_summary} · {marker}"
                                      if c.fact_summary else marker)
                break


async def _stamp_corrections(store: AsyncCosmosStore,
                             citations: list[Citation]) -> list[Citation]:
    """Fetch-and-stamp convenience for the fact-serving lookup tools."""
    if citations:
        corrected = await _corrected_fields_by_doc(
            store, sorted({c.doc_id for c in citations if c.doc_id}))
        _stamp_corrected_citations(citations, corrected)
    return citations


# ---------------------------------------------------------------------------
# Cross-reference expansion
# ---------------------------------------------------------------------------


async def expand_cross_refs(
    *, store: AsyncCosmosStore, section_ids: list[str], k: int = 12,
    role: str | None = None,
) -> list[Citation]:
    """For each section in ``section_ids``, follow the reference evidence
    (``target_section_id`` fields — the old REFERENCES_SECTION edges) and
    return evidence spans from the target sections. Used when the agent has
    already located a clause and wants to know what it points to.
    Confidential spans are filtered at the source for uncleared roles."""
    if not section_ids:
        return []
    refs = await store.query(
        "SELECT c.target_section_id FROM c WHERE c.kind = 'evidence' AND "
        "IS_DEFINED(c.target_section_id) AND ARRAY_CONTAINS(@sids, c.section_id)",
        [{"name": "@sids", "value": list(section_ids)}])
    targets = sorted({r["target_section_id"] for r in refs
                      if r.get("target_section_id")})
    if not targets:
        return []
    evs = await store.query(
        f"SELECT {_cols(_EVIDENCE_COLS)} FROM c WHERE c.kind = 'evidence' AND "
        "ARRAY_CONTAINS(@tids, c.section_id)",
        [{"name": "@tids", "value": targets}])
    evs = [e for e in evs if _span_visible(e, role)][:k]
    rows = await _enrich_evidence(store, evs, {e["id"]: 0.85 for e in evs},
                                  with_links=False)
    return [_row_to_citation(r, default_score=0.85) for r in rows]


# ---------------------------------------------------------------------------
# Tool 8 — Orphan-fragment recall (the provenance floor)
# ---------------------------------------------------------------------------
#
# Orphan mentions are raw harvested spans that categorise/normalise dropped —
# they back NO typed fact and are invisible to every tool above (not
# embedded, no fact link). This tool is the recall net for the fragment
# that's demonstrably in the document but didn't survive canonicalization.
# Keyword match (no embedding = no API cost); boilerplate / edge-only raw
# labels are excluded so the net stays high-signal.

_ORPHAN_WORD_RE = re.compile(r"[A-Za-z0-9]{4,}")
# Only labels handled by a DEDICATED path are excluded outright: 'reference'
# (becomes reference evidence) and 'identifier' (matched by id lookups).
# We deliberately do NOT blanket-exclude 'other'/'unknown'/'' — the LLM dumps
# substantive provisions there (governing law, liability caps, a doc title, an
# engineering-deliverables list) alongside form furniture. Filtering by LABEL
# threw the provisions out with the furniture; we filter by CONTENT instead
# (see _orphan_is_substantive) so the provisions become reachable while the
# furniture ("Signed by") still doesn't.
_ORPHAN_NOISE_LABELS = ["reference", "identifier"]
# A dropped span is worth surfacing only if it carries real content. Form labels
# the model tagged 'other' ("Signed by", "Additional Remarks if any:") are short
# and number-free; provisions are longer, and dropped measurements carry a digit.
_ORPHAN_MIN_CHARS = 15


def _orphan_is_substantive(text: str) -> bool:
    """True when an orphan span is worth offering as last-resort evidence: it
    holds a value (any digit — preserves the dropped-measurement use) OR enough
    prose to be a real provision. Pure; unit-tested. Filters the form-label
    furniture that shares the 'other' bucket with genuine provisions."""
    t = (text or "").strip()
    if not t:
        return False
    if any(ch.isdigit() for ch in t):
        return True
    return len(t) >= _ORPHAN_MIN_CHARS


def _query_words(query: str) -> list[str]:
    """Distinct lowercased 4+ char tokens from the query — the keyword set
    matched against orphan span text. Pure; tested directly."""
    return sorted({w.lower() for w in _ORPHAN_WORD_RE.findall(query or "")})


async def find_orphan_fragments(
    *, store: AsyncCosmosStore, query: str,
    raw_labels: list[str] | None = None,
    doc_ids: list[str] | None = None, k: int = 6,
    role: str | None = None,
) -> list[Citation]:
    """Keyword recall over ORPHAN mentions — raw spans categorise/normalise
    dropped, so they back no typed fact and no other tool can see them.
    LAST-RESORT net: use only when something is clearly in the document (a
    measurement or number, a clause fragment, or a whole provision the model
    couldn't categorise — governing law, a liability cap, a deliverables list)
    but every typed lookup and vector search came up empty. Matches any 4+ char
    query word; admits the 'other' bucket but filters out form furniture by
    content. Returns lower-confidence citations (the spans were dropped for a
    reason) — verify against the snippet before relying on them."""
    words = _query_words(query)
    if not words:
        return []
    params: list[dict] = [
        {"name": "@exclude", "value": _ORPHAN_NOISE_LABELS},
    ]
    label_clause = "true"
    if raw_labels:
        params.append({"name": "@labels", "value": list(raw_labels)})
        label_clause = "ARRAY_CONTAINS(@labels, c.raw_label)"
    contains = _contains_any_clause("text_span", words[:12], params, "w")
    scope = _scope_sql(doc_ids, params)
    mentions = await store.query(
        f"SELECT * FROM c WHERE c.kind = 'mention' AND c.surfaced = false "
        f"AND NOT ARRAY_CONTAINS(@exclude, c.raw_label) AND {label_clause} "
        f"AND {contains} AND {scope}", params)
    mentions = [m for m in mentions if _span_visible(m, role)]

    def _hits(m: dict) -> int:
        t = (m.get("text_span") or "").lower()
        return sum(1 for w in words if w in t)

    mentions.sort(key=lambda m: (-_hits(m), len(m.get("text_span") or "")))
    sections = await _fetch_by_ids(
        store, [m["section_id"] for m in mentions[:k * 4]
                if m.get("section_id")], kind="section")
    out: list[Citation] = []
    for m in mentions[:max(k * 4, 12)]:
        if not _orphan_is_substantive(m.get("text_span")):
            continue
        hits = _hits(m)
        s = sections.get(m.get("section_id")) or {}
        out.append(_row_to_citation({
            "evidence_id": m["id"],
            "citation_kind": "fact_mention",
            "canonicalized": False,
            "confidence_tier": "raw",
            "raw_label": m.get("raw_label"),
            "doc_id": m.get("doc_id"),
            "page_no": m.get("page_no"),
            "snippet": m.get("text_span"),
            "bbox_x0": m.get("bbox_x0"), "bbox_y0": m.get("bbox_y0"),
            "bbox_x1": m.get("bbox_x1"), "bbox_y1": m.get("bbox_y1"),
            "rects_json": m.get("rects_json"),
            "section_id": s.get("id"),
            "section_num": s.get("section_num"),
            "section_title": s.get("title"),
            "fact_labels": [],
            "fact_props": None,
            "score": 0.70 if hits >= 5 else (0.45 + 0.05 * hits),
        }, default_score=0.45))
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------------------
# Tool 8b — Document assets (schematics / drawings / figure pages)
# ---------------------------------------------------------------------------
#
# A plant schematic carries almost no prose, so no text tool can find it. The
# KB build detects figure-dominant pages deterministically (docasset items —
# doc, page, title like "ATTACHMENT 4: COOLING WATER SECTION") and this tool
# matches a question against those titles, returning a page-level citation
# the UI can open the PDF at. Pointing, not reading: the tool says WHERE the
# diagram is.

_DIAGRAM_WORDS = frozenset({
    "schematic", "schematics", "diagram", "diagrams", "drawing", "drawings",
    "figure", "figures", "layout", "layouts", "flowsheet", "flowchart", "plan",
})


def score_asset(row: dict, words: list[str]) -> int:
    """Content-word hits against the asset's title + its document's title.
    Generic diagram words ("schematic", "drawing") are excluded from scoring —
    they say WHAT the user wants, not WHICH one. So a bare 'schematic' matches
    every asset, while naming the subject alongside it ranks that page first.
    Pure, unit-tested."""
    hay = f"{row.get('title') or ''} {row.get('doc_title') or ''}".lower()
    return sum(1 for w in words if w not in _DIAGRAM_WORDS and w in hay)


async def find_document_assets(
    *, store: AsyncCosmosStore, query: str, doc_ids: list[str] | None = None,
    k: int = 6, role: str | None = None,
) -> list[Citation]:
    """Find full-page diagrams/schematics/drawings and return page-level
    citations (no rects — the viewer opens the page itself). Asset pages
    holding a confidential block are invisible to uncleared roles: the
    asset title is OCR text from that page, so surfacing it would leak
    what the block filter hides."""
    words = _query_words(query)
    params: list[dict] = []
    assets = await store.query(
        f"SELECT * FROM c WHERE c.kind = 'docasset' AND {_scope_sql(doc_ids, params)}",
        params)
    if assets and not _text_cleared(role):
        conf_params: list[dict] = [{"name": "@sens", "value": "confidential"}]
        conf_scope = _scope_sql(sorted({a["doc_id"] for a in assets}), conf_params)
        conf = await store.query(
            "SELECT c.doc_id, c.page_no FROM c WHERE c.kind = 'block' AND "
            f"c.sensitivity = @sens AND {conf_scope}", conf_params)
        conf_pages = {(b["doc_id"], int(b.get("page_no") or 0)) for b in conf}
        assets = [a for a in assets
                  if (a["doc_id"], int(a.get("page_no") or 0)) not in conf_pages]
    titles = await build_doc_titles(store, sorted({a["doc_id"] for a in assets}) or None)
    rows = [{
        "asset_id": a.get("asset_id") or a["id"], "doc_id": a["doc_id"],
        "page_no": a.get("page_no"), "title": a.get("title"),
        "kind": a.get("asset_kind"),
        "doc_title": titles.get(a["doc_id"]) or a["doc_id"],
    } for a in assets]
    rows.sort(key=lambda r: (r["doc_id"], r.get("page_no") or 0))
    scored = sorted(((score_asset(r, words), r) for r in rows),
                    key=lambda t: (-t[0], t[1]["doc_id"], t[1]["page_no"]))
    out: list[Citation] = []
    for hits, r in scored[:k]:
        out.append(Citation(
            evidence_id=f"asset:{r['asset_id']}",
            citation_kind="document_asset",
            confidence_tier="layout",
            doc_id=r["doc_id"], page_no=int(r["page_no"] or 1),
            section_title=r["title"],
            snippet=f"{r['title']} — full-page {r['kind']}, page {r['page_no']}",
            score=0.5 + 0.1 * hits,
        ))
    return out


# ---------------------------------------------------------------------------
# Tool 9 — Keyword / lexical recall (the exact-token arm of hybrid retrieval)
# ---------------------------------------------------------------------------
#
# Dense vectors blur exact and rare tokens: model numbers, part numbers,
# ratings, acronyms, clause labels, phrases quoted verbatim. Those are exactly
# the tokens a technical or legal document turns on. This tool
# fetches CONTAINS candidates and ranks them with the deterministic
# keyword_score (BM25-shaped), returning the same Citation atoms as every
# other tool. It complements vector_search_evidence; the agent's bundle then
# fuses both via RRF.


async def keyword_search(
    *, store: AsyncCosmosStore, query: str, k: int = 8,
    doc_ids: list[str] | None = None, role: str | None = None,
) -> list[Citation]:
    """Lexical recall over evidence text UNION raw paragraph text. The
    exact-token partner to vector_search_evidence: use it for model/spec
    strings, amperages, acronyms, clause labels, or a verbatim phrase the
    user quoted — anything where the literal characters matter more than the
    meaning. Evidence hits rank first (they carry fact context); raw block
    hits follow as the safety net for uncaptured wording."""
    qtokens = tokenize(query)
    if not qtokens:
        return []
    out: list[Citation] = []

    # Arm 1: evidence spans.
    params: list[dict] = []
    contains = _contains_any_clause("text_span", qtokens[:12], params, "w")
    scope = _scope_sql(doc_ids, params)
    evs = await store.query(
        f"SELECT TOP 200 * FROM c WHERE c.kind = 'evidence' AND {contains} "
        f"AND {scope}", params)
    evs = [e for e in evs if _span_visible(e, role)]
    scored_evs = sorted(
        ((keyword_score(qtokens, e.get("text_span") or ""), e) for e in evs),
        key=lambda t: -t[0])[:k]
    rows = await _enrich_evidence(
        store, [e for _, e in scored_evs],
        {e["id"]: max(s, 0.6) for s, e in scored_evs})
    out.extend(_row_to_citation(r, default_score=0.6) for r in rows)

    # Arm 2: raw blocks — catches wording no fact captured, and the only
    # lexical path on schema-first docs (no evidence spans).
    params = []
    contains = _contains_any_clause("text", qtokens[:12], params, "w")
    scope = _scope_sql(doc_ids, params)
    blocks = await store.query(
        f"SELECT TOP 200 * FROM c WHERE c.kind = 'block' AND {contains} "
        f"AND {scope}", params)
    blocks = [b for b in blocks if _block_visible(b, role)]
    scored_blocks = sorted(
        ((keyword_score(qtokens, b.get("text") or ""), b) for b in blocks),
        key=lambda t: -t[0])[:k]
    sections = await _sections_of_blocks(store, [b for _, b in scored_blocks])
    seen_pages = {(c.doc_id, c.page_no) for c in out}
    for s, b in scored_blocks:
        score = max(s, 0.5)
        # A raw block on a page an evidence hit already covers ranks under
        # it — the typed span beats its own paragraph.
        if (b.get("doc_id"), int(b.get("page_no") or 0)) in seen_pages:
            score = min(score, 0.55)
        out.append(_row_to_citation(
            _block_row(b, sections.get(b.get("section_id")), score),
            default_score=0.5))
    return out[: 2 * k]


# ---------------------------------------------------------------------------
# OpenAI tool-call schemas
# ---------------------------------------------------------------------------
#
# Names match the keys in ``TOOL_DISPATCH``. The agent loop hands these to
# ``openai.chat.completions.create(tools=...)`` and dispatches the resulting
# tool_calls by name.


# Skipped when no domain is configured yet (DEFAULT_DOCTYPE == "", see
# pipeline/ontology.py): _id_key_by_label() calls load_pack() with no
# doctype, which would try to open configs/packs/.yaml and crash this
# module's import before the setup wizard ever gets a chance to run. A
# restart always follows the wizard finishing (see api/routes/setup.py),
# so this module gets re-imported with a real domain once one exists —
# nothing here is ever actually served against the empty sentinel.
_LABEL_ENUM = sorted(_id_key_by_label()) if DEFAULT_DOCTYPE else []


OPENAI_TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "vector_search_evidence",
            "description": (
                "Fuzzy semantic recall over EvidenceSpans. Use when no direct "
                "label/section lookup fits — open-ended questions, paraphrased "
                "concepts, or 'find anything about X'. Also returns "
                "low-confidence raw FactMention fragments when query terms "
                "overlap dropped fine-grained spans."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "The text to embed and search. "
                                             "Use the user's wording + key entities."},
                    "k":     {"type": "integer", "minimum": 1, "maximum": 30,
                              "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vector_search_sections",
            "description": (
                "Section-level semantic recall. Use for vague navigational "
                "questions ('what does Schedule 1B cover?', 'is there a "
                "clause about X?') where the answer is a whole section."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k":     {"type": "integer", "minimum": 1, "maximum": 10,
                              "default": 4},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vector_search_blocks",
            "description": (
                "Block-level semantic recall over raw paragraph / figure / "
                "table text. CALL THIS FIRST for any question about a diagram, "
                "schematic, figure, floor plan, or chart — the answer is RAW "
                "BLOCK TEXT inside the figure and is NOT in any typed fact, "
                "so `vector_search_evidence` will miss it. Examples: 'what "
                "does the electrical schematic show?', 'what's in Figure 1A?', "
                "'describe the floor plan', 'what equipment appears in the "
                "diagram?'. Also useful as a fallback when other vector "
                "searches return weak hits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k":     {"type": "integer", "minimum": 1, "maximum": 10,
                              "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "keyword_search",
            "description": (
                "Lexical recall over evidence text AND raw paragraph "
                "text — the exact-token partner to vector_search_evidence. "
                "Reach for it when the LITERAL CHARACTERS matter more than "
                "meaning: a model or spec string, a part number, a rating, "
                "an acronym, or a phrase the user quoted verbatim. Because it "
                "also searches raw "
                "paragraphs, it can find wording NO extracted field ever "
                "captured — the safety net when structured lookups return "
                "nothing. Often worth calling ALONGSIDE vector search — the "
                "bundle fuses both."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "The exact terms / phrase to match."},
                    "k":     {"type": "integer", "minimum": 1, "maximum": 30,
                              "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_section",
            "description": (
                "Exact / prefix lookup for a clause number ('9.3', '9', "
                "'7.1(b)'). Use when the user names a section. Prefix match: "
                "'9' returns 9, 9.1, 9.2, ... — pass the most specific number "
                "the user gave."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section_num": {"type": "string",
                                    "description": "e.g. '9.3' or '7.1(b)'"},
                    "k":           {"type": "integer", "minimum": 1, "maximum": 50,
                                    "default": 20},
                },
                "required": ["section_num"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_defined_term",
            "description": (
                "Lookup a capitalised defined term, the kind a document "
                "defines once and then uses throughout. Returns the "
                "document's own definition. Use "
                "for intent=definition or whenever the question hinges on a "
                "specific defined term's meaning. When the graph has "
                "CanonicalTerm links, this can expand to same-named terms "
                "across documents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {"type": "string",
                             "description": "The term as the user wrote it; "
                                            "casing is normalised internally."},
                    "k":    {"type": "integer", "minimum": 1, "maximum": 10,
                             "default": 5},
                },
                "required": ["term"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_verified_fields",
            "description": (
                "The ALIGNED, human-reviewed knowledge base — one named field "
                "per document (schema-first extraction + review). CALL THIS "
                "FIRST for any question about a NAMED field: amounts, rates "
                "and fees, party names, dates, quantities, responsibilities, "
                "or how one document relates to another. Values are "
                "current-only (superseded statements excluded) and each result "
                "is tagged HUMAN-VERIFIED or AI-extracted — repeat that tag in "
                "your answer so the reader knows the trust level. Search with "
                "the user's own topic words. Falls back cleanly: if this "
                "returns nothing, fall back to vector search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Field name, topic, or the user's own words."},
                    "include_superseded": {
                        "type": "boolean", "default": False,
                        "description": "Also return older superseded statements (history questions)."},
                    "k": {"type": "integer", "minimum": 1, "maximum": 30, "default": 12},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aggregate_ops_fields",
            "description": (
                "TRUE numeric aggregation over the ALIGNED knowledge base "
                "(the human-reviewed ontology fields) — sum/avg/min/max/count "
                "a field's numeric values across CURRENT statements, corpus-"
                "wide or per contract family. Use this whenever "
                "the quantity is a named ontology field (a total, an average or "
                "an extreme of one field across documents) — values "
                "are per-field-supersedence current and trust-tagged. `field` "
                "takes plain words; it resolves to the best ontology field. "
                "op='list' returns the per-document values without arithmetic."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    # No example field name here on purpose: the schema is
                    # per-deployment, so any name written in would be fiction on
                    # every domain but the one it came from. The resolver takes
                    # the words as the user said them.
                    "field": {"type": "string",
                              "description": "The field in plain words, or its full "
                                             "'category.field_key'."},
                    "op":    {"type": "string",
                              "enum": ["sum", "avg", "min", "max", "count", "list"],
                              "default": "sum"},
                    "group": {"type": "string",
                              "description": "Optional contract-family (Document.group) "
                                             "to scope to one family."},
                },
                "required": ["field"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "expand_cross_refs",
            "description": (
                "Follow REFERENCES_SECTION edges from sections already in "
                "your evidence bundle. Use when the user asks 'what does "
                "clause X refer to?' or when a clause you found mentions "
                "another section you haven't looked at yet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "section_id values from prior tool "
                                       "results. The tool walks "
                                       "REFERENCES_SECTION edges out of these.",
                    },
                    "k": {"type": "integer", "minimum": 1, "maximum": 30,
                          "default": 12},
                },
                "required": ["section_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_orphan_fragments",
            "description": (
                "LAST-RESORT recall over raw harvested spans that were "
                "dropped during fact extraction (orphans — they back no "
                "typed fact, so no other tool can see them). Use ONLY when "
                "something clearly exists in the document but every typed "
                "lookup and vector search returned nothing — e.g. an obscure "
                "measurement or stray number, OR a whole provision the "
                "extractor could not categorise (governing law, dispute "
                "resolution / arbitration, a liability cap, an insurance or "
                "deliverables list, the document title). Lower confidence: the "
                "spans were dropped for a reason, so read the snippet before "
                "trusting it. Pass the user's key terms as the query."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Key terms to keyword-match "
                                             "against the raw spans."},
                    "raw_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional harvest-label filter, e.g. "
                                       "['measurement'] or ['money','rate']. "
                                       "Omit to search all non-boilerplate "
                                       "labels.",
                    },
                    "k": {"type": "integer", "minimum": 1, "maximum": 20,
                          "default": 6},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_document_assets",
            "description": (
                "Find full-page DIAGRAMS, SCHEMATICS, DRAWINGS or figure "
                "pages inside the documents: site plans, system drawings, "
                "flow sheets, layouts, detail views. These pages are images "
                "with "
                "almost no text, so NO other tool can find them. Use whenever "
                "the user asks WHERE a diagram/schematic/drawing/layout is, "
                "or asks to see or be directed to one. Returns the document + "
                "page as a citation — cite it so the user can open the page. "
                "The tool points at the diagram; it cannot read its contents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "What the user is looking for, "
                                             "in their own words."},
                    "k": {"type": "integer", "minimum": 1, "maximum": 20,
                          "default": 6},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Signal you have enough evidence to answer. Call this when "
                "the citation bundle covers the question, or when further "
                "tool calls won't add useful evidence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rationale": {"type": "string",
                                  "description": "One sentence: why you're "
                                                 "stopping (for debugging)."},
                },
                "required": ["rationale"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool — the ALIGNED / VERIFIED knowledge base (schema-first extraction + review)
# ---------------------------------------------------------------------------
#
# Queries opsfield items — the per-document ops-view schema fields the ingestion
# + review pipeline produces (LLM fills the named field with evidence; a human
# verifies; kb/km.py aligns them with per-field supersedence + trust). This is
# the AUTHORITATIVE surface for "what is <named field>" questions: values are
# apples-to-apples across documents, stale statements are excluded (is_current),
# and every value carries its trust tier so the answer can say whether a human
# stood behind it.

# The chat layer's viewer roles (general / finance) predate the account roles
# (default / confidential / admin). Map them onto the ontology's sensitivity
# model so a confidential field never reaches a general viewer AT THE SOURCE.
_CHAT_ROLE_SEES_CONFIDENTIAL = {"finance", "admin", "confidential"}


def _ops_visible_item(o: dict, role: str | None) -> bool:
    if (role or "").lower() in _CHAT_ROLE_SEES_CONFIDENTIAL:
        return True
    return (o.get("sensitivity") or "general") != "confidential"


# Qualifier / filler words stripped from the query before token matching —
# they describe HOW to answer ("current", "latest"), not WHICH field.
_OPS_STOPWORDS = {
    "the", "a", "an", "of", "for", "in", "on", "to", "and", "or", "is", "are",
    "what", "whats", "which", "who", "how", "much", "many", "per", "under",
    "current", "latest", "most", "recent", "now", "today", "contract", "agreement",
}


def _ops_row(o: dict, d: dict | None) -> dict:
    return {
        "ops_id": o.get("ops_id") or o["id"], "doc_id": o["doc_id"],
        "title": o.get("title"), "category": o.get("category"),
        "value": o.get("value"), "unit": o.get("unit"),
        "trust": o.get("trust"), "verified": o.get("verified"),
        "confidence": o.get("confidence"), "verifiers": o.get("verifiers"),
        "n_votes": o.get("n_votes"), "disputed": bool(o.get("disputed")),
        "is_current": o.get("is_current", True) is not False,
        "superseded_by": o.get("superseded_by"),
        "snippet": o.get("snippet"), "page": o.get("page"),
        "rects": o.get("rects"),
        "doc_title": (d or {}).get("title") or o["doc_id"],
        "eff": (d or {}).get("effective_date") or "",
        "numbers": o.get("numbers"),
        "carried_from": o.get("carried_from"),
    }


def _ops_row_to_citation(r: dict) -> Citation:
    rects = None
    try:
        parsed = json.loads(r["rects"]) if r.get("rects") else None
        if isinstance(parsed, list) and parsed:
            rects = parsed
    except (json.JSONDecodeError, TypeError):
        pass
    if r.get("trust") == "human_validated":
        trust_tag = "HUMAN-VERIFIED ✓"
        conf, n_votes = r.get("confidence"), int(r.get("n_votes") or 0)
        if conf and n_votes:
            approvers = len(r.get("verifiers") or []) or round(float(conf) * n_votes)
            trust_tag += f" {round(float(conf) * 100)}% ({approvers}/{n_votes} verifiers)"
    elif r.get("disputed"):
        trust_tag = "DISPUTED: verifiers disagree on this value"
    else:
        trust_tag = "AI-extracted, unreviewed"
    currency = "" if r.get("is_current") else " · SUPERSEDED by a later document"
    summary = (f"[{trust_tag}{currency}] {r['title']} = {r['value']}"
               f" ({str(r.get('category') or '').replace('_', ' ')})")
    if r.get("carried_from"):
        summary += (f" · carried over from {r['carried_from']} "
                    f"(unchanged by the pinned document)")
    return Citation(
        evidence_id=f"ops:{r['ops_id']}",
        citation_kind="evidence_span",
        confidence_tier="canonical",
        doc_id=r["doc_id"],
        page_no=int(r["page"] or 1),
        snippet=(r.get("snippet") or str(r.get("value") or ""))[:300],
        bbox=(rects[0]["bbox"] if rects else None),
        rects=rects,
        fact_label="OpsField",
        fact_id=r["ops_id"],
        fact_summary=summary,
        score=0.97 if r.get("verified") else 0.93,
        field_value=(str(r["value"]) if r.get("value") not in (None, "") else None),
        **_review_consensus(r),
    )


async def build_ops_scope(store: AsyncCosmosStore,
                          pinned_doc_ids: list[str] | None) -> dict | None:
    """OPS-layer family expansion for a USER-pinned document set, computed
    ONCE per chat request (chat.py), not per tool call and not per row.

    A pinned amendment inherits: a field the amendment does not re-state
    lives on another family document as its is_current row, so the ops tools
    admit CURRENT rows from unpinned family siblings, marked carried-over.
    Raw-text and fact tools stay strictly pinned (they carry per-clause text,
    not per-field currency, so expansion there would leak sibling clauses)."""
    if not pinned_doc_ids:
        return None
    docs = await store.query(
        "SELECT c.doc_id, c.title, c['group'] AS grp FROM c "
        "WHERE c.kind = 'document'")
    pinned = list(pinned_doc_ids)
    pinned_set = set(pinned)
    groups = {d.get("grp") for d in docs
              if d["doc_id"] in pinned_set and d.get("grp")}
    expanded = list(pinned)
    for d in docs:
        if d.get("grp") in groups and d["doc_id"] not in pinned_set:
            expanded.append(d["doc_id"])
    return {
        "pinned": pinned,
        "doc_ids": expanded,
        "titles": {d["doc_id"]: d.get("title") or d["doc_id"] for d in docs},
    }


def _ops_admit(cands: list[dict], ops_scope: dict | None) -> list[dict]:
    """Pinned-scope admission for OPS rows. Pinned docs keep every candidate
    row. Unpinned family siblings contribute ONLY is_current rows, marked so
    the serialized output says the value carried over unchanged. Aggregates
    over the admitted set are correct by construction (current-only)."""
    if not ops_scope:
        return cands
    pinned = set(ops_scope["pinned"])
    titles = ops_scope.get("titles") or {}
    out: list[dict] = []
    for o in cands:
        if o["doc_id"] in pinned:
            out.append(o)
        elif o.get("is_current", True) is not False:
            out.append({**o,
                        "carried_from": titles.get(o["doc_id"]) or o["doc_id"]})
    return out


async def _ops_candidates(store: AsyncCosmosStore, doc_ids: list[str] | None,
                          include_superseded: bool, role: str | None) -> list[dict]:
    params: list[dict] = []
    rows = await store.query(
        f"SELECT * FROM c WHERE c.kind = 'opsfield' AND {_scope_sql(doc_ids, params)}",
        params)
    out = []
    for o in rows:
        if not include_superseded and o.get("is_current", True) is False:
            continue
        if not _ops_visible_item(o, role):
            continue
        out.append(o)
    return out


async def lookup_verified_fields(
    *, store: AsyncCosmosStore, query: str, role: str | None = None,
    doc_ids: list[str] | None = None,
    include_superseded: bool = False, k: int = 12,
    ops_scope: dict | None = None,
) -> list[Citation]:
    """Search the aligned knowledge base by field name / topic words. Ranked
    by the deterministic keyword score over title/value/snippet/key/category
    (the Lucene index's replacement), with the old token-majority floor:
    at least one real token must hit. Returns one citation per (document ×
    field) statement, current-only by default, trust-tagged
    ([HUMAN-VERIFIED] vs [AI-extracted]). With a pinned ``ops_scope`` the
    candidate set expands to the pinned docs' families, current-only and
    carried-over-marked for unpinned siblings."""
    tokens = [t for t in re.split(r"[^a-z0-9°$%.]+", (query or "").lower())
              if len(t) >= 2 and t not in _OPS_STOPWORDS]
    if not tokens:
        return []
    scope_ids = ops_scope["doc_ids"] if ops_scope else doc_ids
    cands = _ops_admit(
        await _ops_candidates(store, scope_ids, include_superseded, role),
        ops_scope)
    if not cands:
        return []
    docs = await _fetch_by_ids(store, sorted({o["doc_id"] for o in cands}),
                               kind="document")

    def _blob(o: dict, d: dict | None) -> str:
        return " ".join(str(x) for x in (
            o.get("title"), o.get("field_key"), o.get("category"),
            o.get("value"), o.get("snippet"), (d or {}).get("title")) if x)

    scored = []
    for o in cands:
        d = docs.get(o["doc_id"])
        blob = _blob(o, d).lower()
        matches = sum(1 for t in tokens if t in blob)
        if matches < 1:
            continue
        s = keyword_score(tokens, blob)
        # Trust is a ranking WEIGHT, not just a badge: a human-verified value
        # outranks a similarly-relevant machine read, and a disputed value
        # sinks below both. (Multiplicative — keyword relevance still leads,
        # so an off-topic verified field cannot bury the on-topic answer.)
        if o.get("trust") == "human_validated":
            s *= 1.3
        elif o.get("disputed"):
            s *= 0.75
        scored.append((s, matches, o, d))
    scored.sort(key=lambda t: (-t[0], -t[1],
                               not bool(t[2].get("verified")),
                               -(len((t[3] or {}).get("effective_date") or "")),
                               str(t[2].get("title") or "")))
    return [_ops_row_to_citation(_ops_row(o, d)) for _, _, o, d in scored[:k]]


_OPS_AGG_OPS = ("sum", "avg", "min", "max", "count", "list")


async def aggregate_ops_fields(
    *, store: AsyncCosmosStore, field: str, op: str = "sum",
    group: str | None = None, role: str | None = None,
    doc_ids: list[str] | None = None, ops_scope: dict | None = None,
) -> dict:
    """Deterministic arithmetic over the aligned knowledge base — sum / avg /
    min / max / count / list a field's numeric values across CURRENT statements
    (per contract family or corpus-wide). Exact math in code, never LLM
    estimation. ``field`` accepts the field key or plain words ("energy charge
    fees"); it resolves to the best-matching ontology field first.

    RBAC: confidential fields are excluded for uncleared viewers — the numbers
    AND the fact of them never enter the result."""
    if op not in _OPS_AGG_OPS:
        return {"error": f"unknown op {op!r}; use one of {_OPS_AGG_OPS}"}

    ftoks = [t for t in re.split(r"[^a-z0-9]+", (field or "").lower()) if len(t) >= 2]
    if not ftoks:
        return {"error": "field is required"}

    # 1. Resolve to ONE ontology field key (best token overlap wins).
    cands = await store.query(
        "SELECT DISTINCT c.field_key, c.title FROM c WHERE c.kind = 'opsfield'")
    best_fk, best_score = None, 0
    for c in cands:
        blob = f"{c['field_key']} {c.get('title') or ''}".lower() \
            .replace("_", " ").replace(".", " ")
        score = sum(1 for t in ftoks if t in blob)
        if score > best_score or (score == best_score and best_fk
                                  and c["field_key"] < best_fk):
            if score > 0:
                best_fk, best_score = c["field_key"], score
    if not best_fk:
        return {"error": f"no knowledge-base field matches {field!r}"}

    # 2. Fetch the CURRENT, role-visible statements of that field. A pinned
    # ops_scope expands to the families. Current-only holds for every row, so
    # the arithmetic over the expanded set is correct by construction.
    scope_ids = ops_scope["doc_ids"] if ops_scope else doc_ids
    params: list[dict] = [{"name": "@fk", "value": best_fk}]
    rows_raw = await store.query(
        f"SELECT * FROM c WHERE c.kind = 'opsfield' AND c.field_key = @fk "
        f"AND {_scope_sql(scope_ids, params)}", params)
    rows_raw = [o for o in rows_raw
                if o.get("is_current", True) is not False
                and _ops_visible_item(o, role)]
    rows_raw = _ops_admit(rows_raw, ops_scope)
    docs = await _fetch_by_ids(store, sorted({o["doc_id"] for o in rows_raw}),
                               kind="document")
    if group is not None:
        rows_raw = [o for o in rows_raw
                    if ((docs.get(o["doc_id"]) or {}).get("group")
                        or o["doc_id"]) == group]
    rows = [_ops_row(o, docs.get(o["doc_id"])) for o in rows_raw]
    rows.sort(key=lambda r: r.get("eff") or "", reverse=True)

    # Numbers are pooled PER UNIT, never across units. Summing a $/kWh rate
    # with a $/RTh rate produces a number that means nothing, and the old code
    # then stamped it with whichever unit happened to appear most often, so the
    # model received a confident wrong figure and put it in the answer.
    # `km_query.aggregate` already refuses this on the knowledge surface. Chat
    # is the surface people actually use, so it has to refuse it too.
    items, nums = [], []
    by_unit: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        row_nums = [float(n) for n in (r.get("numbers") or [])]
        nums.extend(row_nums)
        unit_key = str(r.get("unit") or "").strip() or "(none)"
        by_unit[unit_key].extend(row_nums)
        item = {"doc": r.get("doc_title") or r["doc_id"], "value": r.get("value"),
                "unit": r.get("unit"), "numbers": row_nums,
                "verified": bool(r.get("verified"))}
        if r.get("carried_from"):
            item["note"] = (f"carried over from {r['carried_from']} "
                            f"(unchanged by the pinned document)")
        items.append(item)

    out = {
        "field_key": best_fk, "op": op, "group": group,
        "n_numbers": len(nums), "n_docs": len(items),
        "items": items[:20],
        "sample": [_ops_row_to_citation(r) for r in rows[:5]],
    }
    mixed = len(by_unit) > 1
    # count is a pure tally, so it is the one operation that stays meaningful
    # when the units disagree.
    if mixed and op != "count":
        out["value"] = None
        out["unit"] = None
        out["by_unit"] = [
            {"unit": None if u == "(none)" else u,
             "value": round({"sum": sum(ns), "avg": sum(ns) / len(ns),
                             "min": min(ns), "max": max(ns),
                             "count": float(len(ns)), "list": None}[op], 4)
             if op != "list" and ns else None,
             "n_numbers": len(ns)}
            for u, ns in sorted(by_unit.items())
        ]
        out["note"] = ("values span multiple units, so there is no single "
                       "total. Report the per-unit figures in by_unit "
                       "separately, and never add them together.")
        return out

    value: float | None = None
    if nums:
        value = {"sum": sum(nums), "avg": sum(nums) / len(nums),
                 "min": min(nums), "max": max(nums),
                 "count": float(len(nums)), "list": None}[op]
        if value is not None:
            value = round(value, 4)
    only_unit = next(iter(by_unit)) if len(by_unit) == 1 else None
    out["value"] = value
    out["unit"] = None if only_unit in (None, "(none)") else only_unit
    return out


# ---------------------------------------------------------------------------
# Tool dispatch table
# ---------------------------------------------------------------------------


# Each entry: (callable, returns_citations: bool). The agent loop unions
# Citation lists and turns count results into a synthetic citation list.
ToolFn = Callable[..., Awaitable[Any]]

TOOL_DISPATCH: dict[str, ToolFn] = {
    "vector_search_evidence":  vector_search_evidence,
    "vector_search_sections":  vector_search_sections,
    "vector_search_blocks":    vector_search_blocks,
    "keyword_search":          keyword_search,
    "lookup_section":          lookup_section,
    "lookup_defined_term":     lookup_defined_term,
    "lookup_verified_fields":  lookup_verified_fields,
    "aggregate_ops_fields":    aggregate_ops_fields,
    "expand_cross_refs":       expand_cross_refs,
    "find_orphan_fragments":   find_orphan_fragments,
    "find_document_assets":    find_document_assets,
}


# ---------------------------------------------------------------------------
# Tool result serialisation (sent back to the model)
# ---------------------------------------------------------------------------
#
# Compact representation — IDs + section anchors + capped snippets. The
# full Citation (with embeddings or bboxes) doesn't help the agent reason.


def _serialize_citation(c: Citation, snippet_cap: int = 300,
                        doc_titles: dict[str, str] | None = None) -> dict:
    snippet = (c.snippet or "").strip()
    if len(snippet) > snippet_cap:
        snippet = snippet[:snippet_cap] + "…"
    # Document identity — WITHOUT this the agent is blind to which contract a
    # result came from and silently mixes near-identical template-twins.
    doc = (doc_titles or {}).get(c.doc_id) or c.doc_id
    out: dict[str, Any] = {
        "evidence_id": c.evidence_id,
        "doc": doc,
        "citation_kind": c.citation_kind,
        "confidence_tier": c.confidence_tier,
        "section_num": c.section_num,
        "section_title": c.section_title,
        "page_no": c.page_no,
        "fact_label": c.fact_label,
        "fact_summary": c.fact_summary,
        "raw_label": c.raw_label,
        "snippet": snippet,
        "score": round(c.score, 3),
    }
    return {k: v for k, v in out.items() if v is not None and v != ""}


def serialize_tool_result(name: str, result: Any,
                          doc_titles: dict[str, str] | None = None) -> str:
    """JSON string for the ``role=tool`` reply. Stays compact so context
    doesn't balloon across loop iterations. ``doc_titles`` stamps each
    citation with its document so the agent can see (and avoid) cross-contract
    mixing."""
    # A failed tool comes back as {"error": ...} regardless of its normal
    # shape — serialize it as-is or the citation path below chokes on it.
    if isinstance(result, dict) and "error" in result:
        return json.dumps({"error": result["error"]}, ensure_ascii=False)
    if name == "aggregate_ops_fields":
        payload = {
            "field_key": result.get("field_key"),
            "op": result.get("op"),
            "value": result.get("value"),
            "unit": result.get("unit"),
            "n_docs": result.get("n_docs"),
            "n_numbers": result.get("n_numbers"),
            "items": result.get("items") or [],
            "sample": [_serialize_citation(c, doc_titles=doc_titles)
                       for c in result.get("sample", [])],
        }
        # When the values span more than one unit there IS no single total, so
        # `value` is null. Without the per-unit rows and the note travelling
        # with it, the model sees a null and improvises one, which is the
        # wrong answer this refusal exists to prevent.
        if result.get("by_unit"):
            payload["by_unit"] = result["by_unit"]
        if result.get("note"):
            payload["note"] = result["note"]
        return json.dumps(payload, ensure_ascii=False)
    if name == "finish":
        return json.dumps({"ok": True}, ensure_ascii=False)
    citations: list[Citation] = result or []
    return json.dumps({
        "n_results": len(citations),
        "results": [_serialize_citation(c, doc_titles=doc_titles) for c in citations],
    }, ensure_ascii=False)
