"""Identifier-driven lookups: exact/prefix section numbers, defined terms,
the (currently dormant) human-correction net over the legacy fact tier, and
cross-reference expansion from a known set of sections.
"""
from __future__ import annotations

import re
from collections import defaultdict

from pipeline.store.aio import AsyncCosmosStore

from ..schemas import Citation
from ._shared import (
    _BLOCK_COLS,
    _EVIDENCE_COLS,
    _SECTION_COLS,
    _block_row,
    _block_visible,
    _cols,
    _enrich_evidence,
    _row_to_citation,
    _scope_sql,
    _sections_of_blocks,
    _span_visible,
)


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
