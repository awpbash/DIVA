"""Tool dispatch table and tool-result serialisation (sent back to the
model).

Compact representation — IDs + section anchors + capped snippets. The
full Citation (with embeddings or bboxes) doesn't help the agent reason.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from ..schemas import Citation
from .lookup import expand_cross_refs, lookup_defined_term, lookup_section
from .ops import aggregate_ops_fields, lookup_verified_fields
from .orphans import find_document_assets, find_orphan_fragments
from .search import (
    keyword_search,
    vector_search_blocks,
    vector_search_evidence,
    vector_search_sections,
)

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
