"""api/routes/feedback.py — in-app user feedback capture.

Any logged-in user can file a report (UI/UX, extraction, wrong answer, other)
with auto-attached client context (tab, thread, last Q/A, document) and up to
three screenshots; admins triage them in the dashboard (new → acknowledged →
fixed). Identity and timestamps are stamped SERVER-side from the session — the
client is never trusted with who-said-what.

Screenshots flow: the client uploads images first (`POST /feedback/upload`,
multipart, images only, server-generated random names under
storage/feedback/), then files the report referencing those names. The names
are unguessable and only admins can read them back.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pipeline.config import Config

from .. import appdb
from .auth import current_user, require_admin

router = APIRouter(prefix="/feedback", tags=["feedback"])

_CFG = Config.load()

# Screenshot intake limits: enough for "here's what I saw", too small to abuse.
_MAX_ATTACHMENTS = 3
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_IMAGE_TYPES = {
    "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif",
}
# Server-generated names only — this shape is the whole path-traversal defence.
_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|webp|gif)$")
_MEDIA = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}


def _attach_dir() -> Path:
    d = _CFG.storage_root / "feedback"
    d.mkdir(parents=True, exist_ok=True)
    return d


class FeedbackIn(BaseModel):
    category: str
    message: str
    context: dict | None = None
    # Names returned by /feedback/upload (validated again here).
    attachments: list[str] | None = None


class StatusBody(BaseModel):
    status: str


@router.post("/upload")
async def upload_attachments(files: list[UploadFile],
                             user: dict = Depends(current_user)) -> dict:
    """Store feedback screenshots (images only) and return their server names.
    Called before /feedback so the report can reference them."""
    if len(files) > _MAX_ATTACHMENTS:
        raise HTTPException(400, f"at most {_MAX_ATTACHMENTS} screenshots per report")
    names: list[str] = []
    for f in files:
        ext = _IMAGE_TYPES.get((f.content_type or "").lower())
        if not ext:
            raise HTTPException(400, "only PNG, JPEG, WEBP or GIF images are accepted")
        # Capped WHILE reading. The size check used to run after the whole
        # file was already in memory, three times over, which is not a limit.
        data = b""
        while chunk := await f.read(1024 * 1024):
            data += chunk
            if len(data) > _MAX_IMAGE_BYTES:
                raise HTTPException(413, "image too large (8 MB max)")
        if not data:
            raise HTTPException(400, "empty image")
        name = f"{uuid.uuid4().hex}.{ext}"
        (_attach_dir() / name).write_bytes(data)
        names.append(name)
    return {"ok": True, "names": names}


@router.post("")
def create_feedback(body: FeedbackIn, user: dict = Depends(current_user)) -> dict:
    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(400, "message is required")
    ctx = json.dumps(body.context, ensure_ascii=False) if body.context else None
    atts = [n for n in (body.attachments or [])
            if _NAME_RE.match(n) and (_attach_dir() / n).exists()][:_MAX_ATTACHMENTS]
    fid = appdb.add_feedback(user["email"], body.category, msg, ctx,
                             json.dumps(atts) if atts else None)
    return {"ok": True, "id": fid}


@router.get("/mine")
def my_feedback(user: dict = Depends(current_user)) -> dict:
    """The reporter's own recent feedback with its triage status — closes the
    loop (people see whether a report was acknowledged or fixed)."""
    items = [{"id": it["id"], "created_at": it["created_at"],
              "category": it["category"], "message": it["message"],
              "status": it["status"]}
             for it in appdb.list_feedback(None)
             if it["email"] == user["email"]][:20]
    return {"items": items}


@router.get("/admin")
def list_feedback(status: str | None = None,
                  user: dict = Depends(require_admin)) -> dict:
    everything = appdb.list_feedback(None)
    counts = {"new": 0, "acknowledged": 0, "fixed": 0}
    for it in everything:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    items = [it for it in everything if not status or it["status"] == status]
    for it in items:                     # JSON columns back to values for the client
        if it.get("context"):
            try:
                it["context"] = json.loads(it["context"])
            except (json.JSONDecodeError, TypeError):
                it["context"] = None
        if it.get("attachments"):
            try:
                it["attachments"] = json.loads(it["attachments"])
            except (json.JSONDecodeError, TypeError):
                it["attachments"] = None
    return {"items": items, "counts": counts}


@router.get("/admin/attachment/{name}")
def get_attachment(name: str, user: dict = Depends(require_admin)) -> FileResponse:
    """Serve one feedback screenshot to the admin dashboard."""
    if not _NAME_RE.match(name):
        raise HTTPException(404, "no such attachment")
    p = _attach_dir() / name
    if not p.exists():
        raise HTTPException(404, "no such attachment")
    return FileResponse(p, media_type=_MEDIA[name.rsplit(".", 1)[1]])


@router.put("/admin/{fid}")
def set_status(fid: int, body: StatusBody,
               user: dict = Depends(require_admin)) -> dict:
    if not appdb.set_feedback_status(fid, body.status, user["email"]):
        raise HTTPException(404, "no such feedback item (or invalid status)")
    appdb.log_event(user["email"], "feedback", f"marked {body.status}",
                    target=f"feedback #{fid}")
    return {"ok": True}
