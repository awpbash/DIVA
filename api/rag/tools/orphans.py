"""Last-resort recall tools: keyword search over dropped orphan mentions,
and page-level lookup of full-page diagrams/schematics/drawings, content
no other tool can see because it carries no embedding and no fact link.
"""
from __future__ import annotations

import re

from pipeline.store.aio import AsyncCosmosStore

from ..schemas import Citation
from ._shared import (
    _contains_any_clause,
    _fetch_by_ids,
    _row_to_citation,
    _scope_sql,
    _span_visible,
    _text_cleared,
)
from .catalog import build_doc_titles


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
