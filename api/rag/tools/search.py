"""Vector and keyword recall tools: evidence, section and block level
semantic search, plus the lexical (exact-token) partner to vector search.
"""
from __future__ import annotations

from openai import AsyncOpenAI

from pipeline.store.aio import AsyncCosmosStore
from pipeline.store.query import keyword_score, tokenize

from ..schemas import Citation
from ._shared import (
    _EVIDENCE_COLS,
    _block_row,
    _block_visible,
    _cols,
    _contains_any_clause,
    _embed,
    _enrich_evidence,
    _fetch_by_ids,
    _rank_citations,
    _row_to_citation,
    _scope_sql,
    _sections_of_blocks,
    _span_visible,
)
from .orphans import find_orphan_fragments


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
