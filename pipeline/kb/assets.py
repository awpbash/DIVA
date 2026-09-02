"""kb/assets.py — page-level visual assets (schematics, drawings, figure pages).

A plant schematic carries almost no prose, so nothing indexed it and chat was
blind to it ("where is the cooling water schematic?" → nothing). This pass
detects figure-dominant pages DETERMINISTICALLY from the cached OCR layer
(storage/pages_md — no re-ingest, no LLM) and materialises them as
``:DocAsset`` nodes so a retrieval tool can point the user at the exact page.

Detection, tuned against the real corpus: a page qualifies when its ``figure``
blocks cover ≥ 25% of the page. Real diagram pages measure 0.30–0.84; inline
logos and formula images measure ≤ 0.09, so the gate has a wide margin. The
title is the page's heading (else its caption) — e.g. "ATTACHMENT 4: COOLING
WATER SECTION".
"""
from __future__ import annotations

import json

from ..config import Config
from ..store import model
from .writers import _store

_FIG_AREA_MIN = 0.25
_KIND = "diagram"


def page_asset(page: dict) -> dict | None:
    """One cached OCR page → an asset row, or None. Pure — unit-tested."""
    blocks = page.get("blocks") or []
    figs = [b for b in blocks if b.get("kind") == "figure"]
    if not figs:
        return None
    area = 0.0
    for b in figs:
        bb = b.get("bbox") or []
        if len(bb) == 4:
            area += max(0.0, bb[2] - bb[0]) * max(0.0, bb[3] - bb[1])
    if area < _FIG_AREA_MIN:
        return None
    page_no = int(page.get("page_no") or 0)
    if page_no < 1:
        return None

    def _first(kind: str) -> str:
        return next((str(b.get("text") or "").strip() for b in blocks
                     if b.get("kind") == kind and str(b.get("text") or "").strip()),
                    "")

    title = _first("heading") or _first("caption") or f"Diagram on page {page_no}"
    return {"page_no": page_no, "title": title[:160], "kind": _KIND,
            "area": round(area, 3)}


def scan_doc(cfg: Config, doc_id: str) -> list[dict]:
    """Every asset page of one document, from its cached OCR pages."""
    pages_dir = cfg.storage_root / "pages_md" / doc_id
    if not pages_dir.is_dir():
        return []
    out: list[dict] = []
    for pj in sorted(pages_dir.glob("p_*.json")):
        try:
            page = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        asset = page_asset(page)
        if asset:
            out.append(asset)
    return out


def build_assets(cfg: Config) -> dict:
    """(Re)materialise every loaded document's assets. Idempotent — clear +
    re-upsert per doc, so a re-OCR'd document sheds stale pages."""
    counts = {"docs": 0, "assets": 0}
    store = _store(cfg)
    doc_ids = [r["doc_id"] for r in store.query(
        "SELECT c.doc_id FROM c WHERE c.kind = 'document'")]
    for doc_id in doc_ids:
        rows = scan_doc(cfg, doc_id)
        for stale in store.query(
                "SELECT c.id FROM c WHERE c.kind = 'docasset'", pk=doc_id):
            store.delete(stale["id"], doc_id)
        if not rows:
            continue
        store.upsert_many([
            model.item(model.DOCASSET, f"{doc_id}:p{r['page_no']}", doc_id, {
                "asset_id": f"{doc_id}:p{r['page_no']}", "doc_id": doc_id,
                "page_no": r["page_no"], "title": r["title"],
                "asset_kind": r["kind"],
            }) for r in rows
        ])
        counts["docs"] += 1
        counts["assets"] += len(rows)
    return counts
