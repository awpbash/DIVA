"""Document listing + PDF streaming.

Two endpoints back the document picker and the PDF panel:

* ``GET /docs``         — list ingested docs with a friendly title + page count
* ``GET /pdf/{doc_id}`` — stream the raw PDF bytes to a CLEARED account. The
  frontend wraps the response in a blob URL for react-pdf-highlighter.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from pipeline.kb import km as km_mod
from pipeline.kb.opsview_spec import rel_full_key

from .. import deps
from .. import trust as trust_mod
from ..settings import get_config
from .auth import current_user, require_clearance

# Login required for the catalog. The raw PDF is gated harder, per route: it is
# the unredactable surface, so it takes clearance rather than a login.
router = APIRouter(tags=["docs"], dependencies=[Depends(current_user)])


@router.get("/documents")
async def list_docs() -> dict:
    """Return all docs that are both ingested (canonical exists) and have a
    PDF on disk. The frontend uses this to populate its doc picker."""
    cfg = get_config()
    canonical_dir = cfg.storage_root / "canonical"
    raw_dir = cfg.storage_root / "raw"
    if not canonical_dir.exists():
        return {"docs": []}

    doc_ids = sorted(p.stem for p in canonical_dir.glob("*.json"))

    # Pull display info from the store (n_facts, doctype, family + the
    # document's own stated type/date from the aligned KM fields) — fall
    # back to filesystem.
    store = deps.get_store()
    by_id: dict[str, dict] = {}
    for d in await store.query(
            "SELECT c.doc_id, c.doctype, c.total_pages, c.n_facts, "
            "c['group'] AS grp, c.effective_date FROM c "
            "WHERE c.kind = 'document' AND ARRAY_CONTAINS(@ids, c.doc_id)",
            [{"name": "@ids", "value": doc_ids}]):
        by_id[d["doc_id"]] = {
            "doc_id": d["doc_id"], "doctype": d.get("doctype"),
            "total_pages": d.get("total_pages"), "n_facts": d.get("n_facts"),
            "group": d.get("grp"), "effective_date": d.get("effective_date"),
        }
    for a in await store.query(
            "SELECT c.doc_id, c.title FROM c WHERE c.kind = 'agreement' "
            "AND ARRAY_CONTAINS(@ids, c.doc_id)",
            [{"name": "@ids", "value": doc_ids}]):
        if a["doc_id"] in by_id:
            by_id[a["doc_id"]]["title"] = a.get("title")
    # The two keys come from the active domain's field roles. Writing one
    # domain's spelling here matched nothing on any other, so every document
    # in the catalog showed a blank type and a blank date with no error.
    type_key = rel_full_key("document_type")
    date_key = rel_full_key("document_date")
    # NB: the alias must not be `value` — VALUE is a Cosmos SQL keyword and
    # the emulator's parser rejects it as an alias (SC1001).
    for o in await store.query(
            "SELECT c.doc_id, c.full_key, c['value'] AS field_value FROM c "
            "WHERE c.kind = 'opsfield' AND ARRAY_CONTAINS(@keys, c.full_key) "
            "AND ARRAY_CONTAINS(@ids, c.doc_id)",
            [{"name": "@keys", "value": [type_key, date_key]},
             {"name": "@ids", "value": doc_ids}]):
        row = by_id.get(o["doc_id"])
        if row is None:
            continue
        if o["full_key"] == type_key:
            row["doc_type"] = o.get("field_value")
        else:
            row["doc_date"] = o.get("field_value")

    # Folder names for the family headers: the raw group string stays the
    # grouping key, the folder row (when one exists) supplies the display name.
    try:
        from pipeline.kb import registry as registry_mod
        folder_names = {f["folder_id"]: f["name"]
                        for f in registry_mod.list_folders(cfg, include_inactive=True)}
    except Exception:  # noqa: BLE001 (a missing app.db just means raw group strings)
        folder_names = {}

    review = await trust_mod.review_status_async()
    docs = []
    for doc_id in doc_ids:
        meta = by_id.get(doc_id, {})
        pdf_exists = (raw_dir / f"{doc_id}.pdf").exists()
        t = review.get(doc_id)
        doc_date = meta.get("doc_date")
        docs.append({
            "doc_id":      doc_id,
            "title":       meta.get("title") or meta.get("doctype") or doc_id,
            "doctype":     meta.get("doctype"),
            "total_pages": meta.get("total_pages") or 0,
            "n_facts":     meta.get("n_facts") or 0,
            "has_pdf":     pdf_exists,
            # Family + the document's own stated type/date (from the aligned KM
            # fields) — lets pickers group by contract family and sort by date.
            "group":        meta.get("group"),
            "group_name":   folder_names.get(meta.get("group")),
            "doc_type":     meta.get("doc_type"),
            "doc_date":     doc_date,
            "doc_date_iso": km_mod._to_iso(doc_date) or meta.get("effective_date"),
            # Review trust — the same status the heatmap shows, surfaced where
            # the user picks documents ("referencing reviewed docs is reliable").
            "review_tier":      t["tier"] if t else "unreviewed",
            "review_verified":  t["verified"] if t else 0,
            "review_populated": t["populated"] if t else 0,
        })
    return {"docs": docs}


@router.get("/pdf/{doc_id}")
async def get_pdf(doc_id: str,
                  _user: dict = Depends(require_clearance)) -> FileResponse:
    """Stream the raw PDF, for cleared accounts only.

    The source document is the one surface that cannot be redacted on the way
    out: it is the original bytes, confidential clauses included. Every other
    surface filters server-side (evidence snippets, chat payloads, knowledge
    rows), so leaving this one open to any logged-in account defeated all of
    them in a single request, and the client-side blur is a display convenience
    rather than a control. Default accounts get chat, where role filtering
    applies per retrieval, which is the same line `require_clearance` already
    draws for the knowledge and graph surfaces.
    """
    cfg = get_config()
    # Guard against path traversal — doc_id must be a flat sha-prefix.
    if "/" in doc_id or "\\" in doc_id or "." in doc_id:
        raise HTTPException(400, "invalid doc_id")
    path: Path = cfg.storage_root / "raw" / f"{doc_id}.pdf"
    if not path.exists():
        raise HTTPException(404, f"PDF not found for doc_id={doc_id}")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{doc_id}.pdf",
        headers={"Cache-Control": "public, max-age=3600"},
    )
