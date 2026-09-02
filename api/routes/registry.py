"""api/routes/registry.py — named contract families (Admin only).

A folder NAMES a contract family that already exists structurally: the
folder_id IS the sidecar group string that scopes the amendment DAG, so
renaming one changes display and nothing else. Backed by
pipeline/kb/registry.py (SQLite rows in storage/app.db). Every mutation is
attributed and lands in the activity feed. Folders retire (active=0) instead
of deleting, because documents keep their group either way.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from pipeline.config import Config
from pipeline.kb import registry

from .. import appdb
from .auth import require_admin

router = APIRouter(prefix="/registry", tags=["registry"])

_CFG = Config.load()


# --------------------------------------------------------------------------- #
# Folders: named contract families. The folder_id IS the sidecar group
# string, so a folder names a family that already exists structurally.
# Renaming changes display only. Retiring hides the name, never the docs.
# --------------------------------------------------------------------------- #
@router.get("/folders")
def list_folders(_admin: dict = Depends(require_admin)) -> dict:
    """All folders (retired included, the UI dims them) with the documents
    each one holds. Membership comes from the sidecar groups so documents
    with no intake declaration still show up under their family."""
    try:
        # Families minted since boot (a linked upload) get a row on demand.
        registry.sync_folders_from_groups(cfg=_CFG)
    except Exception:  # noqa: BLE001
        pass
    docs_by_group: dict[str, list[str]] = {}
    for doc_id, group in registry.sidecar_groups(cfg=_CFG).items():
        docs_by_group.setdefault(group, []).append(doc_id)
    folders = []
    for f in registry.list_folders(_CFG, include_inactive=True):
        doc_ids = sorted(docs_by_group.get(f["folder_id"], []))
        folders.append({**f, "doc_ids": doc_ids, "n_docs": len(doc_ids)})
    return {"folders": folders}


class FolderIn(BaseModel):
    name: str


@router.post("/folders")
def create_folder(body: FolderIn, admin: dict = Depends(require_admin)) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    folder_id = registry.mint_folder_id(name)
    registry.upsert_folder(folder_id, name, by=admin["email"], cfg=_CFG)
    appdb.log_event(admin["email"], "registry", "added folder",
                    target=name, detail=folder_id)
    return {"ok": True, "folder_id": folder_id}


class FolderPatch(BaseModel):
    name: str | None = None
    active: bool | None = None


@router.patch("/folders/{folder_id}")
def patch_folder(folder_id: str, body: FolderPatch,
                 admin: dict = Depends(require_admin)) -> dict:
    f = registry.get_folder(folder_id, cfg=_CFG)
    if not f:
        raise HTTPException(404, "no such folder")
    if body.name is not None and not body.name.strip():
        raise HTTPException(400, "name cannot be empty")
    name = (body.name if body.name is not None else f["name"]).strip()
    if body.name is not None:
        registry.upsert_folder(folder_id, name, by=admin["email"], cfg=_CFG)
        appdb.log_event(admin["email"], "registry", "updated folder",
                        target=name, detail=folder_id)
    if body.active is not None:
        registry.set_folder_active(folder_id, body.active,
                                   by=admin["email"], cfg=_CFG)
        appdb.log_event(admin["email"], "registry",
                        "restored folder" if body.active else "retired folder",
                        target=name, detail=folder_id)
    return {"ok": True}
