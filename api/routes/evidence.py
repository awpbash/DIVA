"""Evidence lookup.

Returns the page + bbox + snippet + section context for one citation atom.
The frontend hits this when the user clicks a citation tag in the chat
panel — the response drives the PDF highlight.

A citation id is usually an evidence span (canonical-fact-anchored). It can
also be a mention (``<doc>:m:<raw_id>``) — a raw harvested span the
orphan-fragment recall tool surfaced. Both carry the same page geometry,
so both must resolve here or the highlight breaks on click.

Cosmos note: every citation id is prefixed by its doc_id, which IS the
partition key — so each lookup is a point read.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from .. import deps
from ..rag import policy as policy_mod
from .auth import current_user


router = APIRouter(tags=["evidence"], dependencies=[Depends(current_user)])


def _doc_of(eid: str) -> str:
    return eid.split(":", 1)[0]


async def _resolve_evidence(store, eid: str) -> dict | None:
    e = await store.point(eid, _doc_of(eid))
    if not e or e.get("kind") != "evidence":
        return None
    s = (await store.point(e["section_id"], e["doc_id"])
         if e.get("section_id") else None)
    n = (await store.point(e["fact_id"], e["doc_id"])
         if e.get("fact_id") else None)
    is_ref = e.get("evidence_type") == "inferred_reference"
    return {
        "cites_conf_block": bool(e.get("cites_confidential")),
        "evidence_id": e["id"],
        "citation_kind": "reference" if is_ref else "evidence_span",
        "canonicalized": True,
        "confidence_tier": "reference" if is_ref else "canonical",
        "raw_label": None,
        "doc_id": e["doc_id"], "page_no": e.get("page_no"),
        "snippet": e.get("text_span"),
        "bbox_x0": e.get("bbox_x0"), "bbox_y0": e.get("bbox_y0"),
        "bbox_x1": e.get("bbox_x1"), "bbox_y1": e.get("bbox_y1"),
        "rects_json": e.get("rects_json"),
        "section_id": (s or {}).get("id"),
        "section_num": (s or {}).get("section_num"),
        "section_title": (s or {}).get("title"),
        "fact_labels": (n or {}).get("labels") or [],
        "fact_props": n,
    }


async def _resolve_mention(store, eid: str) -> dict | None:
    m = await store.point(eid, _doc_of(eid))
    if not m or m.get("kind") != "mention":
        return None
    s = (await store.point(m["section_id"], m["doc_id"])
         if m.get("section_id") else None)
    return {
        "cites_conf_block": bool(m.get("cites_confidential")),
        "evidence_id": m["id"],
        "citation_kind": "fact_mention",
        "canonicalized": False,
        "confidence_tier": "raw",
        "raw_label": m.get("raw_label"),
        "doc_id": m["doc_id"], "page_no": m.get("page_no"),
        "snippet": m.get("text_span"),
        "bbox_x0": m.get("bbox_x0"), "bbox_y0": m.get("bbox_y0"),
        "bbox_x1": m.get("bbox_x1"), "bbox_y1": m.get("bbox_y1"),
        "rects_json": m.get("rects_json"),
        "section_id": (s or {}).get("id"),
        "section_num": (s or {}).get("section_num"),
        "section_title": (s or {}).get("title"),
        "fact_labels": [],
        "fact_props": None,
    }


async def _resolve_block(store, eid: str) -> dict | None:
    # Synthetic id: '<doc_id>:block:<block_id>' (block_id = '<doc_id>:bNNNN').
    if ":block:" not in eid:
        return None
    doc_id, block_id = eid.split(":block:", 1)
    b = await store.point(block_id, doc_id)
    if not b or b.get("kind") != "block":
        return None
    s = (await store.point(b["section_id"], doc_id)
         if b.get("section_id") else None)
    return {
        "evidence_id": eid,
        "citation_kind": "block",
        "canonicalized": None,
        "confidence_tier": "layout",
        "raw_label": None,
        "block_sensitivity": b.get("sensitivity") or "general",
        "doc_id": b["doc_id"], "page_no": b.get("page_no"),
        "snippet": b.get("text"),
        "bbox_x0": b.get("bbox_x0"), "bbox_y0": b.get("bbox_y0"),
        "bbox_x1": b.get("bbox_x1"), "bbox_y1": b.get("bbox_y1"),
        "rects_json": None,
        "section_id": (s or {}).get("id"),
        "section_num": (s or {}).get("section_num"),
        "section_title": (s or {}).get("title"),
        "fact_labels": [],
        "fact_props": None,
    }


async def _resolve_heading(store, eid: str) -> dict | None:
    if not eid.endswith(":heading"):
        return None
    section_id = eid[: -len(":heading")]
    s = await store.point(section_id, _doc_of(section_id))
    if not s or s.get("kind") != "section":
        return None
    return {
        "evidence_id": eid,
        "citation_kind": "section_heading",
        "canonicalized": None,
        "confidence_tier": "layout",
        "raw_label": None,
        "doc_id": s["doc_id"], "page_no": s.get("page_start"),
        "snippet": s.get("title") or s.get("section_num"),
        "bbox_x0": s.get("bbox_x0"), "bbox_y0": s.get("bbox_y0"),
        "bbox_x1": s.get("bbox_x1"), "bbox_y1": s.get("bbox_y1"),
        "rects_json": None,
        "section_id": s["id"],
        "section_num": s.get("section_num"),
        "section_title": s.get("title"),
        "fact_labels": [],
        "fact_props": None,
    }


def _to_response(r: dict) -> dict:
    parts = [r.get("bbox_x0"), r.get("bbox_y0"), r.get("bbox_x1"), r.get("bbox_y1")]
    bbox = [float(p) for p in parts] if all(p is not None for p in parts) else None
    rects = None
    if r.get("rects_json"):
        try:
            parsed = json.loads(r["rects_json"])
            rects = parsed if isinstance(parsed, list) and parsed else None
        except json.JSONDecodeError:
            rects = None
    fact_labels = r.get("fact_labels") or []
    return {
        "evidence_id":   r["evidence_id"],
        "citation_kind": r.get("citation_kind"),
        "canonicalized": r.get("canonicalized"),
        "confidence_tier": r.get("confidence_tier"),
        "raw_label":     r.get("raw_label"),
        "doc_id":        r["doc_id"],
        "page_no":       int(r.get("page_no") or 0),
        "snippet":       r.get("snippet") or "",
        "bbox":          bbox,
        "rects":         rects,
        "section_id":    r.get("section_id"),
        "section_num":   r.get("section_num"),
        "section_title": r.get("section_title"),
        "fact_label":    fact_labels[0] if fact_labels else None,
        "fact_props":    dict(r["fact_props"]) if r.get("fact_props") else None,
    }


def _collect_regions(rows: list[dict], out: list[dict]) -> None:
    for r in rows:
        added = False
        if r.get("rects_json"):
            try:
                for rect in json.loads(r["rects_json"]):
                    if isinstance(rect, dict) and rect.get("bbox"):
                        out.append({"page_no": rect.get("page_no", r["page_no"]),
                                    "bbox": rect["bbox"]})
                        added = True
            except (json.JSONDecodeError, TypeError):
                pass
        parts = [r.get("x0"), r.get("y0"), r.get("x1"), r.get("y1")]
        if not added and all(p is not None for p in parts):
            out.append({"page_no": r["page_no"], "bbox": [float(p) for p in parts]})


@router.get("/restricted-regions/{doc_id}")
async def restricted_regions(doc_id: str, user: dict = Depends(current_user)) -> dict:
    """All blur regions for a document under the SESSION role. Empty for a
    cleared role (confidential / admin). The PDF viewer blurs these on every
    page in Restricted view — so a sensitive value can't be read straight
    off the source even when no answer cited it."""
    if "/" in doc_id or "\\" in doc_id:
        raise HTTPException(400, "invalid doc_id")
    role = user.get("role")
    labels = sorted(policy_mod.restricted_labels(role))
    raw_labels = sorted(policy_mod.restricted_raw_labels(role))
    denied = bool(policy_mod.denied_classes(role))
    if not labels and not raw_labels and not denied:
        return {"regions": [], "role": role}
    store = deps.get_store()
    regions: list[dict] = []
    if labels:
        # Restricted typed facts → their evidence geometry.
        facts = await store.query(
            "SELECT c.id FROM c WHERE c.kind = 'fact' AND "
            "ARRAY_CONTAINS(@labs, c.label)",
            [{"name": "@labs", "value": labels}], pk=doc_id)
        fact_ids = {f["id"] for f in facts}
        if fact_ids:
            evs = await store.query(
                "SELECT c.fact_id, c.page_no, c.bbox_x0 AS x0, c.bbox_y0 AS y0, "
                "c.bbox_x1 AS x1, c.bbox_y1 AS y1, c.rects_json FROM c "
                "WHERE c.kind = 'evidence' AND IS_DEFINED(c.fact_id)", pk=doc_id)
            _collect_regions([e for e in evs if e["fact_id"] in fact_ids], regions)
    if raw_labels:
        rows = await store.query(
            "SELECT c.page_no, c.bbox_x0 AS x0, c.bbox_y0 AS y0, "
            "c.bbox_x1 AS x1, c.bbox_y1 AS y1, c.rects_json FROM c "
            "WHERE c.kind = 'mention' AND ARRAY_CONTAINS(@raws, c.raw_label)",
            [{"name": "@raws", "value": raw_labels}], pk=doc_id)
        _collect_regions(rows, regions)
    if denied:
        # Confidential ontology-field evidence (works on schema-first docs
        # where no typed facts exist to carry the geometry).
        rows = await store.query(
            "SELECT c.page AS page_no, null AS x0, null AS y0, null AS x1, "
            "null AS y1, c.rects AS rects_json FROM c WHERE c.kind = 'opsfield' "
            "AND c.sensitivity = 'confidential' AND IS_DEFINED(c.rects)",
            pk=doc_id)
        _collect_regions(rows, regions)
    return {"regions": regions, "role": role}


@router.get("/evidence/{evidence_id}")
async def get_evidence(evidence_id: str, user: dict = Depends(current_user)) -> dict:
    store = deps.get_store()
    row = None
    for resolver in (_resolve_evidence, _resolve_mention,
                     _resolve_block, _resolve_heading):
        row = await resolver(store, evidence_id)
        if row is not None:
            break
    if row is None:
        raise HTTPException(404, f"evidence not found: {evidence_id}")
    resp = _to_response(row)
    # Direct-fetch guard: geometry stays (the viewer draws the blur box there),
    # but restricted TEXT never leaves the server for an uncleared role — via
    # the fact label, the raw harvest label, or the block's own sensitivity tag.
    role = user.get("role")
    uncleared = bool(policy_mod.denied_classes(role))
    uncleared_conf_block = uncleared and (
        row.get("block_sensitivity") == "confidential"   # block-kind citation
        or bool(row.get("cites_conf_block"))             # span/mention citing one
    )
    if (uncleared_conf_block
            or (resp.get("fact_label") in policy_mod.restricted_labels(role))
            or ((resp.get("raw_label") or "").lower()
                in policy_mod.restricted_raw_labels(role))):
        resp["snippet"] = "[restricted for your access level]"
        resp["fact_props"] = None
    return resp
