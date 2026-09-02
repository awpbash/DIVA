"""api/routes/review.py — the human-verification review API (multi-verifier votes).

Serves per-document REVIEW RECORDS (storage/review/<doc>.json, produced by
`python -m eval.extraction.record`) to the Review UI, RBAC-filtered, and accepts
verification VOTES from approved verifiers. Two RBAC dimensions
(the account's role + the admin-managed verifier flag):

  visibility  — Default sees only `general` fields; Confidential + Admin see all.
  capability  — Admins and verifier-flagged accounts may vote / correct evidence
                (enforced here, not in the UI).

Verification is GitHub-review style: each verifier approves the value, rejects it,
or proposes an amendment; other verifiers concur. Votes live append-only in
storage/app.db (api/review_votes.py) and their CONSENSUS — verified/disputed,
confidence = largest vote share, who verified — is compiled into the per-doc
overlay (storage/review/<doc>.verified.json), which stamps the graph
`human_validated` and survives re-extraction.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pipeline.config import Config
from pipeline.kb import km as km_mod
from pipeline.kb.opsview_spec import load as load_view
from pipeline.kb.opsview_spec import order_categories
from pipeline.kb.writers import _store, strip_system
from pipeline.store import model as store_model

from .. import appdb, review_votes
from .. import trust as trust_mod
from .auth import require_clearance, require_verifier

# Every route here is for APPROVED VERIFIERS (admins + accounts an admin flagged;
# server-side, from the login session): the review screen is where humans stamp
# trust onto the KB. What each verifier SEES is still clearance-filtered per
# field (`_can_see`), so a Default-role verifier never sees confidential fields.
router = APIRouter(prefix="/review", tags=["review"],
                   dependencies=[Depends(require_verifier)])

_CFG = Config.load()
_REVIEW = _CFG.storage_root / "review"
_PAGES = _CFG.storage_root / "pages"
def _can_see(field: dict, role: str) -> bool:
    """Visibility: Default is filtered from `confidential` fields; the rest see all."""
    if role in ("admin", "confidential"):
        return True
    return str(field.get("sensitivity") or "general") != "confidential"


def _overlay_path(doc_id: str) -> Path:
    return _REVIEW / f"{doc_id}.verified.json"


def _load_overlay(doc_id: str) -> dict:
    p = _overlay_path(doc_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Returning {} here would let the next vote's read-modify-write wipe
        # every other field's verification. Fail loud instead.
        raise HTTPException(500, "verification overlay unreadable, restore from filestore") from None


def _safe(doc_id: str) -> str:
    if not doc_id.isalnum():
        raise HTTPException(400, "bad doc_id")
    return doc_id


@router.get("/docs")
def list_review_docs(user: dict = Depends(require_verifier)) -> dict:
    """Every document with a review record + its status counts + verify progress.

    `n_populated` counts role-visible fields that actually carry a value (vs
    "Not Stated"). Docs are returned richest-first so the picker defaults to a
    base contract with data to review — not a thin amendment that legitimately
    states almost nothing (its blanks *inherit* from the base, they aren't losses).
    """
    r = user["role"]                # clearance-filters what this verifier sees
    # Folder membership so the overview can group by contract family. The
    # sidecar group is the ground truth; the folder row supplies the name.
    try:
        from pipeline.kb import intake as intake_mod
        from pipeline.kb import registry as registry_mod
        fmap = {f["folder_id"]: f["name"]
                for f in registry_mod.list_folders(_CFG, include_inactive=True)}
    except Exception:  # noqa: BLE001 — no registry: docs list stays flat
        intake_mod, fmap = None, {}
    out: list[dict] = []
    if _REVIEW.exists():
        for p in sorted(_REVIEW.glob("*.json")):
            if p.name.endswith(".verified.json"):
                continue
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            overlay = _load_overlay(rec.get("doc_id", ""))
            all_fields = rec.get("fields") or {}
            visible = [f for f in all_fields.values() if _can_see(f, r)]
            grp = None
            if intake_mod is not None:
                try:
                    grp = intake_mod.load_group(_CFG, rec.get("doc_id", ""))
                except Exception:  # noqa: BLE001
                    grp = None
            out.append({
                "doc_id": rec.get("doc_id"), "title": rec.get("title"),
                "group": grp, "group_name": fmap.get(grp) or grp,
                "status_counts": rec.get("status_counts", {}),
                "n_fields": len(all_fields),
                "n_visible": len(visible),
                "n_populated": sum(1 for f in visible if f.get("values")),
                # Overlay keys can outlive their field (ontology rename, record
                # regeneration): count only entries the record still knows.
                "n_verified": sum(1 for k, v in overlay.items()
                                  if k in all_fields and v.get("verified")),
                "n_needs_correction": sum(
                    1 for k, v in overlay.items()
                    if k in all_fields and v.get("needs_correction")),
            })
    out.sort(key=lambda d: (-d["n_populated"], d.get("title") or ""))
    return {"role": r, "docs": out}


@router.get("/heatmap")
def review_heatmap(user: dict = Depends(require_verifier)) -> dict:
    """A documents × categories verification matrix for the review overview.

    Per (document, category) cell: how many fields carry a value (`populated`) and how
    many of those a human has verified (`verified`). The UI colours the cell by the
    verified/populated ratio, so at a glance you see which contracts are reviewed
    (green), extracted-but-unchecked (amber), or empty (grey).
    """
    r = user["role"]                # clearance-filters what this verifier sees
    titles = load_view().category_titles
    docs_out: list[dict] = []
    if _REVIEW.exists():
        for p in sorted(_REVIEW.glob("*.json")):
            if p.name.endswith(".verified.json"):
                continue
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            overlay = _load_overlay(rec.get("doc_id", ""))
            cells: dict[str, dict] = {}
            n_pop = n_ver = 0
            for fk, f in (rec.get("fields") or {}).items():
                if not _can_see(f, r):
                    continue
                if not f.get("values") and not (overlay.get(fk, {}).get("added")):
                    continue                                   # not stated → not "data to review"
                cell = cells.setdefault(f.get("category"), {"populated": 0, "verified": 0})
                cell["populated"] += 1
                n_pop += 1
                if overlay.get(fk, {}).get("verified"):
                    cell["verified"] += 1
                    n_ver += 1
            docs_out.append({
                "doc_id": rec.get("doc_id"), "title": rec.get("title"),
                "n_populated": n_pop, "n_verified": n_ver, "cells": cells,
            })
    docs_out.sort(key=lambda d: (-d["n_populated"], d.get("title") or ""))
    # Only categories some visible document actually populates (keeps the grid tight
    # + respects RBAC — a Default view drops the all-confidential columns entirely).
    present = {c for d in docs_out for c in d["cells"]}
    cats = [{"key": c, "title": titles.get(c, c.replace("_", " "))}
            for c in order_categories(present)]
    return {"role": r, "categories": cats, "docs": docs_out}


# One definition of an evidence span's identity, shared with the KM builder so the
# live correction path and a full rebuild derive the same state.
_ev_key = km_mod.ev_key


def _apply_evidence_overlay(f: dict, ov: dict | None) -> dict:
    """Merge the reviewer's evidence corrections into a record field, read-time:
    drop evidence spans the reviewer rejected as the wrong clause, and append the
    values/clauses a reviewer attached by hand (kind='human'). The record file on
    disk stays machine-pure; the overlay is the human layer."""
    if not ov:
        return f
    rejected = set(ov.get("rejected_evidence") or [])
    added = ov.get("added") or []
    if not rejected and not added:
        return f
    f = {**f}
    values = []
    for v in f.get("values") or []:
        # km.ev_rejected is the one matcher both sides share: legacy keys strip
        # field-wide (grandfathered), v2 keys require the rect fingerprint and
        # the owning value to agree — twin table rows never collide.
        evs = [e for e in (v.get("evidence") or [])
               if not km_mod.ev_rejected(rejected, page=e.get("page"),
                                         snippet=e.get("snippet"),
                                         rects=e.get("rects"),
                                         value=str(v.get("value") or ""))]
        # A value whose every citation was rejected disappears with them — the
        # reviewer said the clauses don't support it. (Re-verify sets the value.)
        if evs or not (v.get("evidence")):
            values.append({**v, "evidence": evs, "n_mentions": len(evs) or v.get("n_mentions", 0)})
    for a in added:
        values.append({
            "value": a.get("value") or a.get("snippet") or "",
            "n_mentions": 1,
            "evidence": [{"snippet": (a.get("snippet") or "")[:240], "page": a.get("page"),
                          "rects": a.get("rects"), "kind": "human"}],
        })
    f["values"] = values
    if added and f.get("status") == "grey":
        f["status"] = "yellow"          # has human-added data now; still needs a ✓
    return f


@router.get("/doc/{doc_id}")
def get_review_doc(doc_id: str, user: dict = Depends(require_verifier)) -> dict:
    """The review record with the verification consensus merged in: per field the
    standing votes (who approved / rejected / proposed what), the consensus state
    (verified / disputed + confidence = largest vote share), and `my_vote` so the
    UI can show the caller their own standing position."""
    _safe(doc_id)
    r = user["role"]                # clearance-filters what this verifier sees
    p = _REVIEW / f"{doc_id}.json"
    if not p.exists():
        raise HTTPException(404, "no review record yet — extraction has not run for this document")
    rec = json.loads(p.read_text(encoding="utf-8"))
    overlay = _load_overlay(doc_id)
    votes_by_field = review_votes.doc_votes(doc_id)
    names = {u["email"]: (u.get("name") or u["email"]) for u in appdb.list_users()}
    fields: dict[str, dict] = {}
    hidden = 0
    for key, f in (rec.get("fields") or {}).items():
        if not _can_see(f, r):
            hidden += 1
            continue
        ov = overlay.get(key)
        if ov:
            f = {**f, "verified": ov.get("verified", False),
                 "verified_value": ov.get("value"),
                 "verified_evidence": ov.get("evidence"),
                 "verifier": ov.get("verifier"), "verified_at": ov.get("at"),
                 "confidence": ov.get("confidence"), "n_votes": ov.get("n_votes"),
                 "verifiers": ov.get("verifiers") or [],
                 "disputed": bool(ov.get("disputed")),
                 "needs_correction": bool(ov.get("needs_correction")),
                 "stale_approvals": bool(ov.get("stale_approvals")),
                 "pending_value": ov.get("pending_value")}
        votes = [{"voter": v["voter"], "voter_name": names.get(v["voter"], v["voter"]),
                  "decision": v["decision"], "value": v["value"],
                  "comment": v["comment"], "at": v["created_at"],
                  "has_evidence": bool(v.get("evidence"))}
                 for v in votes_by_field.get(key) or []]
        f = _apply_evidence_overlay(f, ov)
        f = {**f, "can_edit": True, "votes": votes,
             "my_vote": next((v for v in votes if v["voter"] == user["email"]), None)}
        fields[key] = f
    return {"doc_id": doc_id, "title": rec.get("title"), "role": r,
            "can_edit": True, "hidden_fields": hidden,
            "quorum": review_votes.MIN_APPROVALS,
            "correction_quorum": review_votes.CORRECTION_APPROVALS,
            "status_counts": rec.get("status_counts", {}), "fields": fields}


@router.get("/blocks/{doc_id}/{page_no}")
def get_blocks(doc_id: str, page_no: int,
               _user: dict = Depends(require_clearance)) -> dict:
    """The selectable text blocks of one page (normalised bbox + text) — powers the
    'add evidence' select mode: the reviewer clicks a clause the AI missed and
    attaches it to a field.

    Cleared accounts only. This returns the page's raw OCR text with no
    filtering, so it hands over exactly the confidential clauses that
    `_can_see` hides from the same caller's field list two endpoints away."""
    _safe(doc_id)
    p = _CFG.storage_root / "pages_md" / doc_id / f"p_{page_no:03d}.json"
    if not p.exists():
        return {"blocks": []}
    try:
        page = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"blocks": []}
    out = [{"bbox": b["bbox"], "text": (b.get("text") or "").strip()}
           for b in (page.get("blocks") or [])
           if b.get("bbox") and (b.get("text") or "").strip()]
    return {"blocks": out}


@router.get("/search/{doc_id}")
def search_doc(doc_id: str, q: str = "",
               _user: dict = Depends(require_clearance)) -> dict:
    """Case-insensitive text search across a document's OCR page blocks — the
    review viewer's find box (jump to the page, flash the clause). Works on
    scanned PDFs because it reads the OCR text, not a PDF text layer.

    Cleared accounts only, for the same reason as `get_blocks`: it returns raw
    document text corpus-wide with no sensitivity filter, so an uncleared
    caller could search for the very values the field list withholds."""
    _safe(doc_id)
    needle = (q or "").strip().lower()
    if len(needle) < 2:
        return {"matches": []}
    base = _CFG.storage_root / "pages_md" / doc_id
    if not base.exists():
        return {"matches": []}
    out: list[dict] = []
    for p in sorted(base.glob("p_*.json")):
        try:
            page = json.loads(p.read_text(encoding="utf-8"))
            page_no = int(p.stem.split("_")[1])
        except (OSError, json.JSONDecodeError, IndexError, ValueError):
            continue
        for b in (page.get("blocks") or []):
            text = (b.get("text") or "").strip()
            if not text or not b.get("bbox"):
                continue
            idx = text.lower().find(needle)
            if idx < 0:
                continue
            start = max(0, idx - 60)
            out.append({"page": page_no, "bbox": b["bbox"],
                        "snippet": text[start:idx + len(needle) + 60].strip()})
            if len(out) >= 80:
                return {"matches": out, "truncated": True}
    return {"matches": out}


@router.get("/page/{doc_id}/{page_no}")
def get_page(doc_id: str, page_no: int) -> FileResponse:
    """Serve a rendered page image (storage/pages/<doc>/p_NNN.png) for the review canvas."""
    _safe(doc_id)
    if page_no < 1 or page_no > 9999:
        raise HTTPException(400, "bad page")
    path = _PAGES / doc_id / f"p_{page_no:03d}.png"
    if not path.exists():
        raise HTTPException(404, "page not found")
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=3600"})


class VoteBody(BaseModel):
    """One verifier's vote. approve with no value endorses the machine value;
    approve with a value proposes (or concurs with) a correction; reject flags
    the value as wrong. A correction may carry the clause that backs it
    (snippet + page + rects) — when the correction wins, that clause becomes
    the field's displayed evidence."""
    decision: Literal["approve", "reject"] = "approve"
    value: str | None = None
    comment: str | None = None
    snippet: str | None = None
    page: int | None = None
    rects: str | None = None          # JSON [{page_no, bbox}] of the selected blocks


def _propagate_to_km(doc_id: str, field_key: str, entry: dict) -> None:
    """Stamp the verification consensus onto the matching :OpsField node so the
    Knowledge Base reflects it IMMEDIATELY (trust tier, confidence, who verified,
    corrected value), instead of waiting for the next KM rebuild. Best-effort: the
    overlay on disk is the source of truth and the next `build_km` re-derives the
    same state, so a down graph must never fail the vote itself."""
    verified = bool(entry.get("verified"))
    value = entry.get("value")
    # Consensus props are sent as explicit empties (not dropped Nones) so a field
    # flipping back to unverified/disputed overwrites its stale stamps on the node.
    props = {
        "trust": "human_validated" if verified else "machine_extracted",
        "verified": verified,
        "verifier": entry.get("verifier"), "verified_at": entry.get("at"),
        "confidence": float(entry.get("confidence") or 0.0),
        "verifiers": entry.get("verifiers") or [],
        "n_votes": int(entry.get("n_votes") or 0),
        "disputed": bool(entry.get("disputed")),
        "needs_correction": bool(entry.get("needs_correction")),
        # Explicit empty list so a flip-back to the machine value clears the
        # displaced stamp left by an earlier winning correction.
        "displaced_values": [],
    }
    if verified and value:
        # The machine values this correction displaced ride on the OpsField for
        # the fallback tools, mirroring km._build_doc_record on a full rebuild.
        rf = _record_field(doc_id, field_key) or {}
        displaced = [str(v.get("value")) for v in (rf.get("values") or [])
                     if v.get("value") is not None
                     and str(v.get("value")) != str(value)]
        props.update({"value": str(value), "values": [str(value)], "n_values": 1,
                      "numbers": km_mod.parse_numbers(value),
                      "unit": km_mod.parse_unit(value),
                      "displaced_values": displaced})
        # A winning correction that carries its own clause re-anchors the
        # field: chat citations must highlight the clause backing the value
        # people actually see, not the machine's superseded one.
        cev = entry.get("evidence") or {}
        if cev.get("rects") or cev.get("snippet"):
            props.update({"snippet": cev.get("snippet"), "page": cev.get("page"),
                          "rects": cev.get("rects"),
                          "has_evidence": bool(cev.get("rects"))})
    try:
        store = _store(_CFG)
        ops_id = f"{doc_id}:{field_key}"
        prior = store.point(ops_id, doc_id)
        if prior is not None:            # SET on MATCH: only update existing
            store.upsert({**strip_system(prior), **props})
        elif verified and value:
            # No node to stamp: the machine never produced this field, or every
            # machine value was withdrawn earlier. A verified correction must
            # still exist in the KB, so rebuild the node from record + overlay.
            _recompute_km_field(doc_id, field_key)
        # Stamping the node is not enough. A correction to "Not Stated" means
        # this document states nothing here, which hands the current value back
        # to an earlier document in the family — and a correction the other way
        # takes it. Only the walk knows that, so re-run it for this one field.
        km_mod.refresh_field_currency(store, doc_id, field_key)
    except Exception:  # noqa: BLE001 — store down: overlay holds; rebuild re-stamps
        pass


def _save_overlay(doc_id: str, overlay: dict) -> None:
    _REVIEW.mkdir(parents=True, exist_ok=True)
    # Atomic replace: a crash mid-write must never leave a torn overlay (the
    # overlay is the doc's entire verification state).
    path = _overlay_path(doc_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(overlay, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    # Human verification is the most valuable data in the system — mirror it
    # to the durable file store immediately (no-op in dev, async in prod).
    from pipeline import filestore
    filestore.push_async(_CFG, ("review",))


def _require_field(doc_id: str, field_key: str, role: str) -> dict:
    """The record field, or 404: missing record, typo'd key, or a field the
    caller's clearance cannot see. Invisible fields 404 exactly like missing
    ones, a write probe must not learn that a confidential field exists."""
    p = _REVIEW / f"{doc_id}.json"
    if not p.exists():
        raise HTTPException(404, "no review record for this document")
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(404, "review record unreadable") from None
    f = (rec.get("fields") or {}).get(field_key)
    if not f or not _can_see(f, role):
        raise HTTPException(404, f"no such field: {field_key}")
    return f


def _record_field(doc_id: str, field_key: str) -> dict | None:
    """Best-effort record field lookup for internal recompiles (no HTTP errors)."""
    p = _REVIEW / f"{doc_id}.json"
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return (rec.get("fields") or {}).get(field_key)


def _machine_digest(field: dict | None) -> str | None:
    """The digest an approve-without-value vote endorses: the field's current
    machine values. None when the record is unavailable (stale check skipped,
    votes stay valid)."""
    if field is None:
        return None
    return review_votes.machine_digest(
        str(v.get("value")) for v in (field.get("values") or [])
        if v.get("value") is not None)


# Two verifiers voting on the same document concurrently must not clobber the
# overlay's read-modify-write (the votes themselves are append-only sqlite —
# always safe). Single-process uvicorn, so a process lock suffices.
_OVERLAY_LOCK = threading.Lock()


def _recompile_field(doc_id: str, field_key: str) -> dict:
    """The single write path every vote funnels through: recompute the field's
    consensus from its standing votes, persist it in the overlay (merging OVER
    the evidence corrections, never wiping them), and push it to the KB live.
    `_recompute_km_field` first restores the machine-derived values on the node
    (so a withdrawn amendment falls back to the extraction), then
    `_propagate_to_km` stamps the consensus on top."""
    digest = _machine_digest(_record_field(doc_id, field_key))
    with _OVERLAY_LOCK:
        overlay = _load_overlay(doc_id)
        entry = review_votes.compile_entry(
            overlay.get(field_key) or {},
            review_votes.current_votes(doc_id, field_key),
            machine_digest=digest)
        overlay[field_key] = entry
        _save_overlay(doc_id, overlay)
    _recompute_km_field(doc_id, field_key)
    _propagate_to_km(doc_id, field_key, entry)
    trust_mod.invalidate()
    return entry


@router.post("/doc/{doc_id}/field/{field_key}/verify")
def vote_field(doc_id: str, field_key: str, body: VoteBody,
               user: dict = Depends(require_verifier)) -> dict:
    """Cast (or change) the logged-in verifier's vote on one field. The field's
    verified/disputed state and its confidence are the consensus of ALL standing
    votes — GitHub-style review, not a single admin's overwrite. The voter is the
    logged-in account: real accountability."""
    _safe(doc_id)
    field = _require_field(doc_id, field_key, user["role"])
    if body.value is not None and not body.value.strip():
        # A blank correction must not silently become a machine-value approval.
        raise HTTPException(400, "blank correction value: omit value to endorse "
                                 "the machine value, or reject it")
    value = (body.value or "").strip() or None
    evidence = None
    if value and ((body.snippet or "").strip() or (body.rects or "").strip()):
        evidence = json.dumps({"snippet": (body.snippet or "").strip()[:240],
                               "page": body.page, "rects": body.rects})
    # An approve of "the machine value" snapshots WHICH value it endorsed, so a
    # re-extraction that changes the value voids the vote instead of inheriting it.
    value_seen = None
    if body.decision == "approve" and value is None:
        value_seen = _machine_digest(field)
    review_votes.cast_vote(
        doc_id, field_key, user["email"], body.decision,
        value=value,
        comment=(body.comment or "").strip() or None,
        evidence=evidence, value_seen=value_seen)
    entry = _recompile_field(doc_id, field_key)
    return {"ok": True, "field": field_key,
            "consensus": {k: entry.get(k) for k in
                          ("verified", "disputed", "needs_correction",
                           "value", "pending_value", "confidence",
                           "verifiers", "n_votes")}}


# --------------------------------------------------------------------------- #
# Evidence corrections — the reviewer fixes the machine's citations.
#   reject: "this value is pinned to the WRONG clause" → detach that span.
#   attach: "the AI missed THIS clause" → link it (with a value) to a field.
# Both persist in the same per-doc overlay (survive re-extraction) and update
# the Knowledge Base node live.
# --------------------------------------------------------------------------- #
class RejectEvidenceBody(BaseModel):
    page: int | None = None
    snippet: str = ""
    rects: str | None = None          # the evidence's rects JSON exactly as served
    value: str | None = None          # the value the reviewer clicked the clause on


class AttachEvidenceBody(BaseModel):
    value: str
    snippet: str = ""
    page: int | None = None
    rects: str | None = None          # JSON [{page_no, bbox}] of the selected blocks


def _recompute_km_field(doc_id: str, field_key: str) -> None:
    """Recompute one field's Knowledge-Base node from the record + overlay (values,
    numbers, first evidence) after an evidence correction. Creates the node when a
    human attach fills a previously-empty field. Best-effort — the overlay is the
    source of truth and the next build_km re-derives the same state."""
    p = _REVIEW / f"{doc_id}.json"
    if not p.exists():
        return
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    f = (rec.get("fields") or {}).get(field_key)
    if not f:
        return
    ov = _load_overlay(doc_id).get(field_key)
    eff = _apply_evidence_overlay(dict(f), ov)
    values = eff.get("values") or []
    value_strings = [str(v.get("value") or "") for v in values if v.get("value")]
    human_only = bool(values) and all(
        (v.get("evidence") or [{}])[0].get("kind") == "human" for v in values)
    # First evidence with a real rectangle wins the highlight slot.
    first_ev = next((e for v in values for e in (v.get("evidence") or [])
                     if e.get("rects")), None) or {}
    props = {
        "doc_id": doc_id, "full_key": field_key,
        "category": f.get("category"), "field_key": field_key.split(".")[-1],
        "title": f.get("title"), "type": f.get("type"),
        "sensitivity": f.get("sensitivity"), "multiplicity": f.get("multiplicity"),
        "mechanism": f.get("method"),
        "value": "; ".join(value_strings), "values": value_strings,
        "n_values": len(value_strings),
        "numbers": [n for vs in value_strings for n in km_mod.parse_numbers(vs)],
        "unit": km_mod.parse_unit(value_strings[0]) if value_strings else None,
        "snippet": first_ev.get("snippet"), "page": first_ev.get("page"),
        "rects": first_ev.get("rects"),
        "has_evidence": bool(first_ev.get("rects")),
    }
    if human_only:
        # A field that exists purely because a human attached it IS human-vouched.
        # Attribute it to whoever attached the evidence (the overlay records `by`).
        by = next((a.get("by") for a in reversed((ov or {}).get("added") or [])
                   if a.get("by")), None)
        props.update({"trust": "human_validated", "verified": True, "verifier": by})
    # A verified correction outlives the machine values: when every machine
    # clause was rejected, the human value (plus its own clause, if marked)
    # IS the field. Deleting the node while the review screen shows the field
    # verified would silently erase the correction from chat.
    entry = ov or {}
    if not value_strings and entry.get("verified") and entry.get("value"):
        cval = str(entry["value"])
        cev = entry.get("evidence") or {}
        # Every machine value was withdrawn by the correction: those raw record
        # values (pre-overlay) are what the winning correction displaced.
        withdrawn = [str(v.get("value")) for v in (f.get("values") or [])
                     if v.get("value") is not None
                     and str(v.get("value")) != cval]
        props.update({
            "value": cval, "values": [cval], "n_values": 1,
            "numbers": km_mod.parse_numbers(cval),
            "unit": km_mod.parse_unit(cval),
            "snippet": cev.get("snippet"), "page": cev.get("page"),
            "rects": cev.get("rects"), "has_evidence": bool(cev.get("rects")),
            "trust": "human_validated", "verified": True,
            "verifier": entry.get("verifier"),
            "displaced_values": withdrawn,
        })
        value_strings = [cval]
    try:
        store = _store(_CFG)
        ops_id = f"{doc_id}:{field_key}"
        if value_strings:
            prior = strip_system(store.point(ops_id, doc_id))
            store.upsert(store_model.item(
                store_model.OPSFIELD, ops_id, doc_id,
                {**prior, "ops_id": ops_id,
                 **{k: v for k, v in props.items() if v is not None}}))
        else:                       # every value lost its evidence → withdraw
            store.delete(ops_id, doc_id)
    except Exception:  # noqa: BLE001 — store down: overlay holds; rebuild re-derives
        pass


@router.post("/doc/{doc_id}/field/{field_key}/reject-evidence")
def reject_evidence(doc_id: str, field_key: str, body: RejectEvidenceBody,
                    user: dict = Depends(require_verifier)) -> dict:
    """Detach one wrongly-cited clause from a field (verifiers — router-gated)."""
    _safe(doc_id)
    _require_field(doc_id, field_key, user["role"])
    with _OVERLAY_LOCK:
        overlay = _load_overlay(doc_id)
        entry = overlay.get(field_key) or {}
        rejected = set(entry.get("rejected_evidence") or [])
        if body.rects and body.value:
            # Precise v2 identity (page+text core, rect fingerprint, owning
            # value): only the clicked rect-twin detaches, and only from the
            # value it was clicked on. Sibling values keep their citations.
            rejected.add(km_mod.ev_key2(body.page, body.snippet,
                                        body.rects, body.value))
        else:
            rejected.add(_ev_key(body.page, body.snippet))   # legacy client
        entry["rejected_evidence"] = sorted(rejected)
        overlay[field_key] = entry
        _save_overlay(doc_id, overlay)
    _recompute_km_field(doc_id, field_key)
    trust_mod.invalidate()
    return {"ok": True, "field": field_key, "rejected": len(rejected)}


@router.post("/doc/{doc_id}/field/{field_key}/attach-evidence")
def attach_evidence(doc_id: str, field_key: str, body: AttachEvidenceBody,
                    user: dict = Depends(require_verifier)) -> dict:
    """Attach a clause the AI missed (with its value) to a field (verifiers)."""
    _safe(doc_id)
    if not body.value.strip():
        raise HTTPException(400, "a value is required")
    _require_field(doc_id, field_key, user["role"])
    with _OVERLAY_LOCK:
        overlay = _load_overlay(doc_id)
        entry = overlay.get(field_key) or {}
        entry.setdefault("added", []).append({
            "value": body.value.strip(), "snippet": (body.snippet or "")[:240],
            "page": body.page, "rects": body.rects,
            "by": user["email"], "at": datetime.now(timezone.utc).isoformat(),
        })
        overlay[field_key] = entry
        _save_overlay(doc_id, overlay)
    _recompute_km_field(doc_id, field_key)
    trust_mod.invalidate()
    return {"ok": True, "field": field_key, "added": len(entry["added"])}
