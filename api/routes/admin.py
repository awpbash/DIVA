"""api/routes/admin.py — the admin dashboard's data + the document pipeline (Admin only).

Two halves:
  * READ — /admin/overview: corpus + per-document status (uploaded → read → extracted →
    in the KB → reviewed), knowledge-base totals, account count.
  * RUN — the integrated document flow. /admin/upload stores a PDF and runs the WHOLE
    chain automatically — full ingestion (OCR, vision correction, tables, generic facts,
    graph load, embeddings) → schema-first field extraction → review record → KM build —
    so an uploaded document becomes retrievable (machine-extracted trust) and lands in the
    review queue by itself; a human review then upgrades fields to human_validated.
    /admin/extract/{doc_id} re-runs the same chain by hand (retry after a failure, or
    re-extract after an ontology change). Jobs run in a background thread; /admin/jobs
    reports progress.

Dev caveat: uvicorn --reload restarts on source edits, which kills an in-flight job — the
stages are all idempotent/cached, so re-clicking Extract resumes where it left off.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import traceback
from datetime import date
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from pydantic import BaseModel

from pipeline.config import Config
from pipeline.kb import intake as intake_mod
from pipeline.kb import registry
from pipeline.kb.opsview_spec import rel_full_key
from pipeline.kb.writers import _store
from pipeline.storage import fields_dir

from .. import appdb, review_votes
from .. import trust as trust_mod
from ..restart import trigger_restart
from .auth import require_admin

router = APIRouter(prefix="/admin", tags=["admin"])

_CFG = Config.load()

# ---- background jobs ----------------------------------------------------------
# In-memory registry is the hot path; every transition ALSO lands in the
# appdb `jobs` table so a container restart can't lose job state. Boot-time
# sweep (main.py lifespan) marks orphaned 'running' rows interrupted; the
# admin retries with the same Extract click (all stages cached — no
# re-spend). Every transition is logged with the doc_id so the job is
# traceable end to end in the container logs.
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()

# Upload cap. An upload used to be read whole into memory with no limit, so one
# authenticated request could take the process down. 200 MB is far above any
# real scanned contract and still bounded.
_MAX_PDF_BYTES = 200 * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024


def _safe_doc_id(doc_id: str) -> str:
    """Same guard api/routes/review.py's _safe() already applies before a
    doc_id reaches a file path: doc_id is always a content hash, never
    anything with a path separator or a `..` segment, so anything that
    doesn't pass isalnum() is not a real id and not worth resolving. These
    routes are admin-only already, but the check is one line and keeps every
    doc_id-taking route in this codebase held to the same bar."""
    if not doc_id.isalnum():
        raise HTTPException(400, "bad doc_id")
    return doc_id


async def _read_capped(upload, limit: int, what: str) -> bytes:
    """Read an upload, refusing once it exceeds `limit`.

    Streamed rather than `await upload.read()`, so an oversized file is
    rejected after one chunk past the limit instead of after the whole thing
    is already resident.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                413, f"{what} too large (limit {limit // (1024 * 1024)} MB)")
        chunks.append(chunk)
    return b"".join(chunks)
# build_km is a clear-then-rebuild of GLOBAL derived state, so two jobs
# interleaving their clears can leave missing hubs/edges. One builder at a time.
_KM_BUILD_LOCK = threading.Lock()
_job_log = logging.getLogger("api.jobs")


def _job_set(doc_id: str, **fields) -> None:
    with _JOBS_LOCK:
        job = _JOBS.setdefault(doc_id, {})
        job.update(fields)
    try:
        appdb.job_upsert(doc_id, **fields)
    except Exception:  # noqa: BLE001 — a locked app.db must not kill the job
        _job_log.warning("job persist failed for %s", doc_id, exc_info=True)
    _job_log.info("job %s | %s", doc_id,
                  " ".join(f"{k}={v}" for k, v in fields.items() if v is not None))


def _job_claim(doc_id: str, **fields) -> bool:
    """Atomic test-and-set: True when the caller won the job slot. Marks the
    job running BEFORE the worker thread starts, so the endpoint can never
    overwrite a transition the worker already made."""
    with _JOBS_LOCK:
        if _JOBS.get(doc_id, {}).get("status") == "running":
            return False
        _JOBS.setdefault(doc_id, {}).update(status="running", **fields)
    try:
        appdb.job_upsert(doc_id, status="running", **fields)
    except Exception:  # noqa: BLE001, a locked app.db must not kill the job
        _job_log.warning("job persist failed for %s", doc_id, exc_info=True)
    _job_log.info("job %s | claimed status=running %s", doc_id,
                  " ".join(f"{k}={v}" for k, v in fields.items() if v is not None))
    return True


def _verified_overlay(doc_id: str) -> dict:
    p = _CFG.storage_root / "review" / f"{doc_id}.verified.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _review_counts(doc_id: str) -> tuple[int, int]:
    """(populated, verified) for a document from its review record + verify overlay."""
    p = _CFG.storage_root / "review" / f"{doc_id}.json"
    if not p.exists():
        return 0, 0
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0, 0
    overlay = _verified_overlay(doc_id)
    pop = ver = 0
    for fk, f in (rec.get("fields") or {}).items():
        if f.get("values"):
            pop += 1
            if overlay.get(fk, {}).get("verified"):
                ver += 1
    return pop, ver


# --------------------------------------------------------------------------- #
# The document pipeline — upload (free read) + extract (paid, one click).
# --------------------------------------------------------------------------- #
def _run_extract_job(doc_id: str, *, refresh_fields: bool = False) -> None:
    """The PAID deliberate half — one click takes the document into EVERY surface:
    full ingestion (chat's text+facts graph), schema-first ops fields, the review
    record, and the KM/Knowledge layer. Every stage is idempotent, so a re-click
    after a failure resumes from cache instead of re-spending.

    `refresh_fields` re-reads the document against the CURRENT field schema.
    The field cache is presence-based, so without it a document extracted
    before a schema edit keeps its old answers forever and the new field comes
    back blank with the job still reporting success. That is indistinguishable
    from the model looking and finding nothing, so it has to be a deliberate
    choice rather than a default: it re-spends on the document."""
    try:
        # Imports live inside the try: an ImportError must land the job in
        # "error", not leave it "running" forever.
        from eval.extraction.record import build_record

        from pipeline.ingest import ingest_doc
        from pipeline.kb import field_llm
        from pipeline.kb import km as km_mod
        from pipeline.kb.opsview_spec import load as load_view

        _job_set(doc_id, status="running", kind="extract", error=None,
                 stage="ingesting (OCR correction, tables, facts, graph)")
        ingest_doc(_CFG, doc_id)

        _job_set(doc_id, stage="re-reading ops fields against the current schema (AI)"
                 if refresh_fields else "extracting ops fields (AI)")
        field_llm.run(doc_id, force=refresh_fields)

        _job_set(doc_id, stage="building review record")
        with _KM_BUILD_LOCK:
            view = load_view()
            store = _store(_CFG)
            doc = store.point(doc_id, doc_id) or {}
            title = doc.get("title") or doc_id
            rec = build_record(store, doc_id, title, view)
            out = _CFG.storage_root / "review" / f"{doc_id}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            # Atomic: the review API serves this file, a torn write mid-job
            # restart would 404 every field of an otherwise healthy document.
            from pipeline.storage import atomic_write_json
            atomic_write_json(out, rec)

            _job_set(doc_id, stage="building knowledge base")
            km_mod.build_km(_CFG)

        trust_mod.invalidate()
        _job_set(doc_id, status="done", stage="in the knowledge base — ready to review")
        # Persist everything this job wrote (PDF, caches, review record) to
        # the durable file mirror. No-op in dev.
        from pipeline import filestore
        filestore.push_async(_CFG)
    except Exception as exc:  # noqa: BLE001
        _job_set(doc_id, status="error", error=f"{exc}")
        _job_log.exception("extract job failed for %s", doc_id)
        traceback.print_exc()


def _validate_intake(relation: str | None, parent_doc_id: str | None,
                     document_type: str | None = None,
                     document_date: str | None = None,
                     doc_id: str | None = None) -> dict:
    """Check the declared intake against the corpus and return the normalised
    intake dict. Document type + date are MANDATORY: they anchor the document
    registry and the date orders the supersedence chain."""
    relation = (relation or "").strip().lower() or None
    if relation and relation not in intake_mod.RELATIONS:
        raise HTTPException(400, f"relation must be one of {intake_mod.RELATIONS}")
    allowed = intake_mod.doc_types()
    document_type = (document_type or "").strip() or None
    if not document_type:
        raise HTTPException(400, "document_type is required: what kind of "
                                 f"document is this? One of {list(allowed)}")
    if document_type not in allowed:
        raise HTTPException(400, f"document_type must be one of {list(allowed)}")
    document_date = (document_date or "").strip() or None
    if not document_date:
        raise HTTPException(400, "document_date is required (the date on the "
                                 "document, YYYY-MM-DD)")
    try:
        # Normalised form stored everywhere: fromisoformat also accepts the
        # compact 20240101, which would otherwise break date ordering in the
        # supersedence chain (string comparison against YYYY-MM-DD).
        document_date = date.fromisoformat(document_date).isoformat()
    except ValueError:
        raise HTTPException(400, "document_date must be a valid YYYY-MM-DD date") from None
    if relation == "standalone":
        # An explicit standalone retires any stale parent from an earlier
        # declaration: a partial edit that only flips the relation must not
        # leave the old parent silently steering the family chain.
        parent_doc_id = None
    if intake_mod.link_relation(relation) and not parent_doc_id:
        raise HTTPException(400, f"a {relation!r} document needs the contract it "
                                 f"{relation} (parent_doc_id)")
    if parent_doc_id and doc_id and parent_doc_id == doc_id:
        raise HTTPException(400, "a document cannot amend, novate or supersede "
                                 "itself — pick a different parent")
    if parent_doc_id and not (_CFG.storage_root / "raw" / f"{parent_doc_id}.pdf").exists():
        raise HTTPException(400, "unknown parent document")
    return {"relation": relation, "parent_doc_id": parent_doc_id or None,
            "document_type": document_type, "document_date": document_date}


def _parent_family(parent_doc_id: str) -> str:
    """The family a linked upload joins. The GRAPH group is the ground truth
    (docmeta derives it from the source folders and CLI-era documents have no
    sidecar at all); the sidecar covers uploads made while the store is asleep,
    and a fresh family keyed on the parent is the last resort."""
    try:
        doc = _store(_CFG).point(parent_doc_id, parent_doc_id)
        g = (doc or {}).get("group")
        if g and not str(g).startswith("fam:"):
            return str(g)
    except Exception:  # noqa: BLE001 — store asleep; fall through to the sidecar
        pass
    return intake_mod.family_for_parent(_CFG, parent_doc_id)


def _parent_folder_conflict(parent_doc_id: str, target_group: str | None) -> None:
    """400 when the declared parent lives in a DIFFERENT real folder than the
    document. Families ARE folders, so a cross-folder link can never form an
    edge — accepting it would silently drop the human declaration (the KM
    build only counts it as unresolved)."""
    if not target_group:
        return
    pg = _parent_family(parent_doc_id)
    if pg and not str(pg).startswith("fam:") and pg != target_group:
        raise HTTPException(400, f"the chosen parent lives in folder {pg!r}, "
                                 f"not {target_group!r} — move the documents "
                                 f"into one folder first, or pick a parent "
                                 f"from this folder")


def _apply_intake(doc_id: str, intake: dict, admin_email: str,
                  group_hint: str | None = None) -> None:
    """Persist the declaration: sidecar first (the durable home), then a
    best-effort push of the family onto the graph so scoping works before the
    next KM build. A linked upload also families its parent. The document's
    own folder (hint or sidecar) outranks the parent-derived family, so an
    ungrouped parent joins the document's folder instead of splitting the
    pair across two families."""
    group = None
    if intake.get("parent_doc_id"):
        group = (group_hint or intake_mod.load_group(_CFG, doc_id)
                 or _parent_family(intake["parent_doc_id"]))
        intake_mod.ensure_group(_CFG, intake["parent_doc_id"], group)
    stamped = {**intake, "declared_by": admin_email,
               "declared_at": intake_mod.now_iso()}
    intake_mod.save_intake(_CFG, doc_id, intake=stamped, group=group)
    # Same declaration, projected into the relational registry (customer →
    # block → document). The sidecar stays the durable home.
    registry.upsert_document(doc_id, stamped, cfg=_CFG)
    if group:
        try:
            store = _store(_CFG)
            for did in (doc_id, intake["parent_doc_id"]):
                doc = store.point(did, did)
                if doc is not None and not doc.get("group"):
                    store.patch(did, did, [
                        {"op": "set", "path": "/group", "value": group}])
        except Exception:  # noqa: BLE001 — store asleep; the KM build stamps it later
            pass


def _run_km_refresh(doc_id: str) -> None:
    """Rebuild just the KM layer (free, no LLM) — for a re-upload that only
    changed the declared intake of an already-extracted document."""
    try:
        from pipeline.kb import km as km_mod

        _job_set(doc_id, status="running", kind="refresh",
                 stage="updating knowledge base (declared details)")
        with _KM_BUILD_LOCK:
            # The KM build carries the whole currency layer now: the declared
            # document DAG and per-field supersedence both live there, so a
            # folder move or relation edit lands in one pass.
            km_mod.build_km(_CFG)
        trust_mod.invalidate()
        _job_set(doc_id, status="done", stage="knowledge base updated")
    except Exception as exc:  # noqa: BLE001
        _job_set(doc_id, status="error", error=f"{exc}")
        traceback.print_exc()


def ingest_local_pdf(path: Path, *, admin_email: str, document_type: str,
                     document_date: str, relation: str | None = None,
                     parent_doc_id: str | None = None) -> str:
    """Ingest a PDF already sitting on local disk, declaring its intake up
    front — the same content-addressed store + background extraction chain
    ``upload_document`` runs for a browser upload, minus the HTTP plumbing.
    Used by the setup wizard's demo-corpus bootstrap (see the lifespan
    handler in api/main.py) so the sample corpus goes through the exact same
    path a real upload does, rather than a second ingestion mechanism to
    keep in sync. Idempotent: re-ingesting the same bytes is a no-op after
    the first time. Returns the doc_id."""
    from pipeline.storage import doc_id_for

    doc_id = doc_id_for(path)
    raw_dir = _CFG.storage_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    dst = raw_dir / f"{doc_id}.pdf"
    already = dst.exists()
    if not already:
        import shutil
        shutil.copyfile(path, dst)
    meta_path = raw_dir / f"{doc_id}.meta.json"
    if not meta_path.exists():
        meta_path.write_text(json.dumps({
            "title": path.stem, "source_path": f"seed:{path.name}", "group": None,
        }, ensure_ascii=False), encoding="utf-8")

    intake = _validate_intake(relation, parent_doc_id, document_type,
                              document_date, doc_id=doc_id)
    if any(intake.values()):
        _apply_intake(doc_id, intake, admin_email)

    already_in_kb = already and (fields_dir(_CFG) / f"{doc_id}.json").exists()
    if not already_in_kb:
        if _job_claim(doc_id, kind="extract", stage="starting…", error=None):
            threading.Thread(target=_run_extract_job, args=(doc_id,), daemon=True).start()
    return doc_id


@router.post("/upload")
async def upload_document(file: UploadFile,
                          relation: str | None = Form(None),
                          parent_doc_id: str | None = Form(None),
                          document_type: str | None = Form(None),
                          document_date: str | None = Form(None),
                          folder_id: str | None = Form(None),
                          _admin: dict = Depends(require_admin)) -> dict:
    """Store an uploaded PDF + the uploader's DECLARED intake (type, date, and
    relationship to an existing document) and run the FULL extraction chain in
    the background. The declaration is human input: it drives the family chain,
    and extraction merely cross-checks it. Re-uploading a known document with a
    new declaration refreshes the knowledge base for free."""
    from pipeline.storage import doc_id_for

    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "only PDF files are supported")
    fid = (folder_id or "").strip() or None
    # Reject an unknown folder up front: the folder_id becomes the family group
    # string, and a typo would silently start a family of one. to_thread: this
    # is a synchronous sqlite read on a route that runs on the shared event
    # loop (see api/appdb.py's _conn() for why that matters).
    if fid and not await asyncio.to_thread(registry.get_folder, fid, cfg=_CFG):
        raise HTTPException(400, f"unknown folder {fid!r}")
    intake = _validate_intake(relation, parent_doc_id,
                              document_type, document_date)
    if intake.get("parent_doc_id") and fid:
        _parent_folder_conflict(intake["parent_doc_id"], fid)
    data = await _read_capped(file, _MAX_PDF_BYTES, "PDF")
    if not data:
        raise HTTPException(400, "empty file")
    # The filename check above only proves the UPLOADER claimed .pdf, not
    # that the bytes are one, so anything renamed to .pdf would otherwise
    # sail through and only fail later, silently, in a background thread.
    # Real PDFs open with this exact magic string.
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, "not a valid PDF file")

    # Content-addressed intake (same id scheme as the CLI): write to a temp name,
    # hash, then move into place. Re-uploading the same contract is a no-op.
    raw_dir = _CFG.storage_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    tmp = raw_dir / f"_upload_{int(time.time()*1000)}.pdf"
    try:
        tmp.write_bytes(data)
        try:
            doc_id = doc_id_for(tmp)
        except Exception:
            raise HTTPException(400, "could not read the PDF") from None
        dst = raw_dir / f"{doc_id}.pdf"
        already = dst.exists()
        if not already:
            # os.replace overwrites atomically. Path.rename raises
            # FileExistsError on Windows when a concurrent identical upload
            # wins the race, stranding the temp file.
            os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)

    # Human metadata sidecar (title = the uploaded filename). Never overwrite a
    # richer sidecar from an earlier CLI intake.
    meta_path = raw_dir / f"{doc_id}.meta.json"
    stem = Path(file.filename).stem
    if not meta_path.exists():
        meta_path.write_text(json.dumps({
            "title": stem, "source_path": f"upload:{file.filename}", "group": None,
        }, ensure_ascii=False), encoding="utf-8")

    if intake.get("parent_doc_id") == doc_id:
        raise HTTPException(400, "a document cannot amend, novate or supersede "
                                 "itself — pick a different parent")
    # The declaration outlives this request: sidecar + (best-effort) graph.
    # Both calls below are synchronous (file writes, a sqlite write, and when
    # a family link is declared, a real blocking Cosmos HTTP round trip via
    # _apply_intake's store.point()/store.patch()), on a route that runs on
    # the shared event loop, so to_thread same as the other db calls in this
    # file (see api/appdb.py's _conn() for why that matters).
    declared = any(intake.values())
    if declared:
        await asyncio.to_thread(
            _apply_intake, doc_id, intake, _admin["email"], group_hint=fid)
    # The chosen folder is the family. Set AFTER the intake push so an
    # explicit folder outranks the parent-derived group of a link relation
    # (the upload dialog filters parents to the folder, so they agree).
    if fid:
        await asyncio.to_thread(intake_mod.set_group, _CFG, doc_id, fid)

    # Auto-extract: a NEW document goes straight through the whole chain (ingest →
    # fields → review record → knowledge base). An already-known document is left
    # alone unless it never made it into the KB (resume via the same idempotent
    # job) — but a fresh declaration still refreshes the KM layer (free).
    already_in_kb = already and (fields_dir(_CFG) / f"{doc_id}.json").exists()
    if not already_in_kb:
        if _job_claim(doc_id, kind="extract", stage="starting…", error=None):
            threading.Thread(target=_run_extract_job, args=(doc_id,), daemon=True).start()
    elif declared:
        if _job_claim(doc_id, kind="refresh", stage="starting…", error=None):
            threading.Thread(target=_run_km_refresh, args=(doc_id,), daemon=True).start()
    if not already:
        await asyncio.to_thread(
            appdb.log_event, _admin["email"], "document", "uploaded",
            target=stem, detail=doc_id)
    return {"ok": True, "doc_id": doc_id, "title": stem, "already_known": already,
            "extracting": not already_in_kb}


@router.post("/extract/{doc_id}")
def extract_document(doc_id: str, refresh_fields: bool = False,
                     _admin: dict = Depends(require_admin)) -> dict:
    """Kick off the paid extraction chain for one document (background).

    `refresh_fields=true` also discards the cached field extraction and reads
    the document again against the current schema. Needed after a schema edit,
    because the cache keys on the file existing and not on what is in it.
    """
    doc_id = _safe_doc_id(doc_id)
    if not (_CFG.storage_root / "raw" / f"{doc_id}.pdf").exists():
        raise HTTPException(404, "no such document")
    if not _job_claim(doc_id, kind="extract", stage="starting…", error=None):
        raise HTTPException(409, "a job is already running for this document")
    threading.Thread(target=_run_extract_job, args=(doc_id,),
                     kwargs={"refresh_fields": refresh_fields},
                     daemon=True).start()
    appdb.log_event(_admin["email"], "document",
                    "re-read fields" if refresh_fields else "ran extraction",
                    target=doc_id)
    return {"ok": True, "doc_id": doc_id, "refresh_fields": refresh_fields}


_INTAKE_KEYS = ("relation", "parent_doc_id",
                "document_type", "document_date")


class IntakePatch(BaseModel):
    """Partial post-upload edit of the declared intake. Only the keys the
    admin sends change. The merged result must still pass the full upload
    validation, so a partial edit can never leave a half-legal declaration."""
    relation: str | None = None
    parent_doc_id: str | None = None
    document_type: str | None = None
    document_date: str | None = None


def _kick_km_refresh(doc_id: str) -> None:
    """The exact free KM-refresh path an intake-only re-upload uses."""
    if not _job_claim(doc_id, kind="refresh", stage="starting…", error=None):
        return
    threading.Thread(target=_run_km_refresh, args=(doc_id,), daemon=True).start()


@router.post("/doc/{doc_id}/intake")
def update_intake(doc_id: str, body: IntakePatch,
                  admin: dict = Depends(require_admin)) -> dict:
    """Edit the declared intake AFTER upload. The patch merges over the
    existing declaration, revalidates with the same rules as upload, and
    persists through the same path (sidecar + registry + family linking +
    free KM refresh). Human input, so it outranks extraction as always."""
    doc_id = _safe_doc_id(doc_id)
    if not (_CFG.storage_root / "raw" / f"{doc_id}.pdf").exists():
        raise HTTPException(404, "no such document")
    existing = intake_mod.load_intake(_CFG, doc_id)
    patch = body.model_dump(exclude_unset=True)
    merged = {k: (patch[k] if k in patch else existing.get(k))
              for k in _INTAKE_KEYS}
    intake = _validate_intake(**merged, doc_id=doc_id)
    if intake.get("parent_doc_id"):
        _parent_folder_conflict(intake["parent_doc_id"],
                                intake_mod.load_group(_CFG, doc_id))
    _apply_intake(doc_id, intake, admin["email"])
    _kick_km_refresh(doc_id)
    appdb.log_event(admin["email"], "document", "updated declared details",
                    target=_doc_title(doc_id),
                    detail=", ".join(f"{k}={v}" for k, v in patch.items()))
    return {"ok": True, "doc_id": doc_id, "intake": intake}


class FolderMove(BaseModel):
    """Target folder for a document. None or empty removes it from its folder."""
    folder_id: str | None = None


@router.post("/doc/{doc_id}/folder")
def move_document_folder(doc_id: str, body: FolderMove,
                         admin: dict = Depends(require_admin)) -> dict:
    """Move a document into (or out of) a folder. The folder_id IS the group
    string, so this sets the sidecar family, pushes it to the graph copy
    best-effort, and refreshes the KM layer for free."""
    doc_id = _safe_doc_id(doc_id)
    if not (_CFG.storage_root / "raw" / f"{doc_id}.pdf").exists():
        raise HTTPException(404, "no such document")
    fid = (body.folder_id or "").strip() or None
    folder = None
    if fid:
        folder = registry.get_folder(fid, cfg=_CFG)
        if not folder:
            raise HTTPException(400, f"unknown folder {fid!r}")
    intake_mod.set_group(_CFG, doc_id, fid)
    # Best-effort push onto the graph copy so retrieval scoping follows
    # before the next KM build (same seam _apply_intake uses).
    try:
        store = _store(_CFG)
        if store.point(doc_id, doc_id) is not None:
            store.patch(doc_id, doc_id, [
                {"op": "set", "path": "/group", "value": fid}])
    except Exception:  # noqa: BLE001
        pass
    _kick_km_refresh(doc_id)
    appdb.log_event(admin["email"], "document",
                    "moved to folder" if fid else "removed from its folder",
                    target=_doc_title(doc_id),
                    detail=(folder or {}).get("name") or fid or "")
    return {"ok": True, "doc_id": doc_id, "folder_id": fid}


@router.get("/jobs")
def jobs(_admin: dict = Depends(require_admin)) -> dict:
    """Current background jobs (upload reads + extractions), for dashboard
    polling. Persisted rows first (they survive restarts and carry the
    'interrupted' state), overlaid with the in-memory registry (fresher for
    an in-flight stage)."""
    out: dict[str, dict] = {}
    try:
        for r in appdb.job_rows():
            out[r["doc_id"]] = {k: r[k] for k in
                                ("kind", "status", "stage", "error") if r.get(k)}
    except Exception:  # noqa: BLE001 — a locked app.db must not fail polling
        pass
    with _JOBS_LOCK:
        for k, v in _JOBS.items():
            out.setdefault(k, {}).update({kk: vv for kk, vv in v.items()
                                          if vv is not None})
    return {"jobs": out}


@lru_cache(maxsize=256)
def _doc_title(doc_id: str) -> str:
    """Human title for the activity feed: the intake sidecar first, then the
    review record (CLI-ingested docs have no sidecar). Cached — titles are
    write-once and the feed resolves one per vote row."""
    for parts in (("raw", f"{doc_id}.meta.json"), ("review", f"{doc_id}.json")):
        p = _CFG.storage_root.joinpath(*parts)
        if p.exists():
            try:
                t = json.loads(p.read_text(encoding="utf-8")).get("title")
                if t:
                    return str(t)
            except (OSError, json.JSONDecodeError):
                pass
    return doc_id[:12]


@router.get("/activity")
def activity(limit: int = 200, _admin: dict = Depends(require_admin)) -> dict:
    """The admin changelog: who did what, when — merged newest-first from the
    three histories that already exist (nothing is double-written):
    verification votes (field_votes), audited mutations (events: ontology,
    accounts, documents, feedback triage) and filed feedback."""
    limit = max(1, min(int(limit), 500))
    names = {u["email"]: (u.get("name") or u["email"]) for u in appdb.list_users()}

    def _field_label(fk: str) -> str:
        cat, _, key = fk.partition(".")
        return f"{key.replace('_', ' ')} ({cat.replace('_', ' ')})" if key else fk

    items: list[dict] = []
    for e in appdb.list_events(limit):
        items.append({
            "at": e["at"], "actor": e["actor"],
            "actor_name": names.get(e["actor"], e["actor"]),
            "kind": e["kind"], "action": e["action"],
            "target": e.get("target") or "", "detail": e.get("detail") or "",
        })
    for v in review_votes.recent_votes(limit):
        action = ("rejected the value" if v["decision"] == "reject"
                  else "proposed a correction" if v.get("value")
                  else "approved the value")
        items.append({
            "at": v["created_at"], "actor": v["voter"],
            "actor_name": names.get(v["voter"], v["voter"]),
            "kind": "verification", "action": action,
            "target": f"{_field_label(v['field_key'])} · {_doc_title(v['doc_id'])}",
            "detail": v.get("value") or v.get("comment") or "",
        })
    for f in appdb.list_feedback(None)[:limit]:
        items.append({
            "at": f["created_at"], "actor": f["email"],
            "actor_name": names.get(f["email"], f["email"]),
            "kind": "feedback", "action": "filed feedback",
            "target": f.get("category") or "",
            "detail": (f.get("message") or "")[:160],
        })
    items.sort(key=lambda x: x["at"], reverse=True)
    return {"items": items[:limit]}


@router.get("/metrics")
def metrics(_admin: dict = Depends(require_admin)) -> dict:
    """Usage metrics: who uses the app and how much. Per-account logins +
    questions + exact token spend (from each answer's TokenMeter), plus a
    daily strip for the recent-activity chart. All from the append-only
    usage_log — sessions can't serve here (logout deletes rows)."""
    users = {u["email"]: u for u in appdb.list_users()}
    rows = []
    totals = {"questions": 0, "prompt_tokens": 0, "completion_tokens": 0, "logins": 0}
    for r in appdb.usage_by_user():
        u = users.get(r["email"], {})
        rows.append({
            **r,
            "prompt_tokens": int(r["prompt_tokens"] or 0),
            "completion_tokens": int(r["completion_tokens"] or 0),
            "name": u.get("name") or r["email"],
            "role": u.get("role") or "?",
        })
        totals["questions"] += int(r["questions"] or 0)
        totals["prompt_tokens"] += int(r["prompt_tokens"] or 0)
        totals["completion_tokens"] += int(r["completion_tokens"] or 0)
        totals["logins"] += int(r["logins"] or 0)
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()[:10]
    totals["active_users_14d"] = len(
        {r["email"] for r in rows if (r.get("last_active") or "") >= cutoff})

    # Dense 7-day axis for the charts (SQL GROUP BY skips quiet days, so a
    # line/bar chart would silently compress time without the zero-fill).
    today = datetime.now(timezone.utc).date()
    days7 = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    sparse = {d["day"]: d for d in appdb.usage_daily(7)}
    daily7 = [{
        "day": day,
        "questions": int(sparse.get(day, {}).get("questions") or 0),
        "tokens": int(sparse.get(day, {}).get("tokens") or 0),
        "logins": int(sparse.get(day, {}).get("logins") or 0),
    } for day in days7]

    per_user: dict[str, dict[str, dict]] = {}
    for r in appdb.usage_daily_by_user(7):
        per_user.setdefault(r["email"], {})[r["day"]] = r
    user_daily = []
    for email, by_day in per_user.items():
        series = [{
            "day": day,
            "questions": int(by_day.get(day, {}).get("questions") or 0),
            "tokens": int(by_day.get(day, {}).get("tokens") or 0),
        } for day in days7]
        u = users.get(email, {})
        user_daily.append({
            "email": email, "name": u.get("name") or email,
            "questions": sum(s["questions"] for s in series),
            "tokens": sum(s["tokens"] for s in series),
            "series": series,
        })
    # Heaviest 7-day spenders first. No cap: the UI shows one person's chart
    # at a time (selector chips), and 7 tiny rows per active user stays small.
    user_daily.sort(key=lambda x: (-x["tokens"], -x["questions"]))

    return {"totals": totals, "users": rows,
            "daily": appdb.usage_daily(14), "daily7": daily7,
            "user_daily": user_daily}


@router.get("/overview")
def overview(_admin: dict = Depends(require_admin)) -> dict:
    """Corpus + per-document pipeline status + KB + account totals for the dashboard."""
    kb: dict[str, dict] = {}
    kb_totals = {"ops_fields": 0, "parties": 0, "families": 0, "human_validated": 0}
    store = _store(_CFG)
    for d in store.query(
            "SELECT c.doc_id, c.title, c['group'] AS grp FROM c "
            "WHERE c.kind = 'document'"):
        kb[d["doc_id"]] = {"title": d.get("title") or d["doc_id"],
                           "group": d.get("grp"), "kb_fields": 0, "kb_verified": 0}
    for o in store.query(
            "SELECT c.doc_id, c.trust FROM c WHERE c.kind = 'opsfield'"):
        row = kb.get(o["doc_id"])
        if row is None:
            continue
        row["kb_fields"] += 1
        kb_totals["ops_fields"] += 1
        if o.get("trust") == "human_validated":
            row["kb_verified"] += 1
            kb_totals["human_validated"] += 1
    kb_totals["parties"] = store.count(
        "SELECT VALUE COUNT(1) FROM c WHERE c.kind = 'hub' AND c.hub = 'CanonicalParty'")
    kb_totals["families"] = len({v["group"] for v in kb.values() if v["group"]})

    # The document's own stated type/date from the aligned KM fields — the
    # fallback when the uploader declared neither (CLI-era docs). Same source
    # /documents uses, so the dashboard and Review agree instead of showing
    # "—" for values Review clearly knows. (Alias must not be `value`: VALUE
    # is a Cosmos SQL keyword and the emulator rejects it — SC1001.)
    # Keys come from the active domain's field roles, not from one domain's
    # spelling: the wrong name matches no rows and the dashboard silently
    # shows a blank type and date for every document.
    type_key = rel_full_key("document_type")
    date_key = rel_full_key("document_date")
    km_meta: dict[str, dict] = {}
    for o in store.query(
            "SELECT c.doc_id, c.full_key, c['value'] AS field_value FROM c "
            "WHERE c.kind = 'opsfield' AND ARRAY_CONTAINS(@keys, c.full_key)",
            [{"name": "@keys", "value": [type_key, date_key]}]):
        row = km_meta.setdefault(o["doc_id"], {})
        if o["full_key"] == type_key:
            row["doc_type"] = o.get("field_value")
        else:
            row["doc_date"] = o.get("field_value")

    ext_dir = fields_dir(_CFG)
    extracted_ids = {p.stem for p in ext_dir.glob("*.json")} if ext_dir.exists() else set()
    raw_dir = _CFG.storage_root / "raw"
    raw_ids = {p.stem for p in raw_dir.glob("*.pdf")} if raw_dir.exists() else set()
    canonical_dir = _CFG.storage_root / "canonical"
    ingested_ids = {p.stem for p in canonical_dir.glob("*.json")} if canonical_dir.exists() else set()
    pages_dir = _CFG.storage_root / "pages_md"
    read_ids = {p.name for p in pages_dir.glob("*") if p.is_dir()} if pages_dir.exists() else set()

    def _sidecar_title(doc_id: str) -> str:
        p = raw_dir / f"{doc_id}.meta.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8")).get("title") or doc_id
            except (OSError, json.JSONDecodeError):
                pass
        return doc_id

    doc_ids = set(kb) | extracted_ids | raw_ids
    declared = registry.list_documents(_CFG)
    docs: list[dict] = []
    for doc_id in sorted(doc_ids):
        populated, verified = _review_counts(doc_id)
        info = kb.get(doc_id, {})
        decl = declared.get(doc_id, {})
        ink = intake_mod.load_intake(_CFG, doc_id)
        in_kb = info.get("kb_fields", 0) > 0
        extracted = doc_id in extracted_ids
        # Pipeline position — drives the dashboard's status column + Extract button.
        status = ("in_kb" if in_kb else
                  "extracted" if extracted else
                  "ingested" if doc_id in ingested_ids else
                  "read" if doc_id in read_ids else
                  "uploaded")
        docs.append({
            "doc_id": doc_id,
            "title": info.get("title") or _sidecar_title(doc_id),
            # Sidecar family first (a folder move lands there instantly),
            # graph copy as the fallback for CLI-era documents.
            "group": intake_mod.load_group(_CFG, doc_id) or info.get("group"),
            "in_kb": in_kb, "extracted": extracted, "status": status,
            "kb_fields": info.get("kb_fields", 0),
            "populated": populated, "verified": verified,
            "doc_type": decl.get("document_type") or km_meta.get(doc_id, {}).get("doc_type"),
            "doc_date": decl.get("document_date") or km_meta.get(doc_id, {}).get("doc_date"),
            # Declared relationship, for prefilling the Organise dialog
            # (lives only in the sidecar intake, not in registry_documents).
            "relation": ink.get("relation"),
            "parent_doc_id": ink.get("parent_doc_id"),
        })
    docs.sort(key=lambda d: (-d["kb_fields"], d["title"]))

    users = appdb.list_users()
    verifier_count = sum(1 for u in users
                        if u.get("role") == "admin" or u.get("verifier"))
    totals = {
        "documents": len(docs),
        "extracted": sum(1 for d in docs if d["extracted"]),
        "in_kb": sum(1 for d in docs if d["in_kb"]),
        "populated": sum(d["populated"] for d in docs),
        "verified": sum(d["verified"] for d in docs),
        "accounts": len(users),
        "verifiers": verifier_count,
        # Surfaced so a multi-verifier instance running with a single-operator
        # quorum setting is visible rather than silent. See review_votes.py's
        # own docstring on why 1 is the right default for a lone operator and
        # the wrong one once a second verifier exists.
        "min_approvals": review_votes.MIN_APPROVALS,
        "correction_approvals": review_votes.CORRECTION_APPROVALS,
        **kb_totals,
    }
    return {"totals": totals, "documents": docs}


# --------------------------------------------------------------------------- #
# Dev / start-over reset — the in-app twin of `python -m scripts.reset_dev`.
# Off by default: a real deployment must not ship a live self-destruct button
# unless whoever runs it deliberately opts in, so it's gated behind an env
# var rather than just an admin role check.
# --------------------------------------------------------------------------- #
_RESET_CONFIRM_PHRASE = "RESET"


def _reset_enabled() -> bool:
    return os.getenv("VERBATIM_ALLOW_RESET", "").strip().lower() in ("1", "true", "yes")


@router.get("/dev-reset")
def dev_reset_status(_admin: dict = Depends(require_admin)) -> dict:
    """Whether the danger-zone "reset this instance" action is available on
    this deployment, and the phrase the UI should ask the admin to type."""
    return {"enabled": _reset_enabled(), "confirm_phrase": _RESET_CONFIRM_PHRASE}


class DevResetBody(BaseModel):
    confirm: str
    wipe_key: bool = False


@router.post("/dev-reset")
def dev_reset(body: DevResetBody, admin: dict = Depends(require_admin)) -> dict:
    """Wipe this instance back to a fresh install and restart into the setup
    wizard. Calls the exact same `perform_reset()` the CLI
    (`scripts/reset_dev.py`) uses — see that module's docstring for exactly
    what gets wiped and what's deliberately left alone. Requires
    VERBATIM_ALLOW_RESET=1 AND the caller to type the confirm phrase back,
    since there is no undo."""
    if not _reset_enabled():
        raise HTTPException(
            403, "instance reset is disabled on this deployment "
                 "(set VERBATIM_ALLOW_RESET=1 to enable it)")
    if body.confirm.strip().upper() != _RESET_CONFIRM_PHRASE:
        raise HTTPException(400, f"type {_RESET_CONFIRM_PHRASE!r} to confirm")
    from scripts.reset_dev import perform_reset
    report = perform_reset(wipe_key=body.wipe_key)
    # Written AFTER the wipe: `events` is truncated, not dropped, by
    # perform_reset(), so this becomes the one row the next admin sees —
    # a clear record that a reset happened, right before this account itself
    # disappears along with every other row `perform_reset` just cleared.
    appdb.log_event(admin["email"], "account", "reset this instance",
                    detail=f"wipe_key={body.wipe_key}")
    trigger_restart()
    return {"ok": True, "restarting": True, "report": report}
