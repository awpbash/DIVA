"""Corpus catalog and document-identity text blocks, injected into the
agent and synth prompts every chat turn so neither is blind to what
documents exist or which one a snippet came from.
"""
from __future__ import annotations

from collections import defaultdict

from pipeline.store.aio import AsyncCosmosStore

from ..resolve import canonical_roles
from ._shared import _scope_sql


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
