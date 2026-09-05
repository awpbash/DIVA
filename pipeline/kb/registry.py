"""kb/registry.py — declared document metadata and named contract families.

Two tables in storage/app.db (same file as accounts and the ontology overlay,
same stdlib-sqlite3 pattern as kb/ontology_store.py):

  * ``registry_documents`` — what the uploader DECLARED about a document at
    intake (its type and date). Human input beats extraction for these.
  * ``registry_folders`` — a name for a contract family. The folder_id IS the
    sidecar ``group`` value that scopes the amendment DAG, so renaming a folder
    never touches a document.

Rows are never hard-deleted: ``active=0`` retires a folder while every document
keeps its group string.

Until 2026-08-30 this module also held a deployment-specific reference-table
registry. That was specific to one installation rather than to the framework,
so it was removed with the rest of the external-reference machinery.
"""
from __future__ import annotations

import re as _re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .. import sqlite_util
from ..config import Config

_DDL = """
CREATE TABLE IF NOT EXISTS registry_documents (
    doc_id        TEXT PRIMARY KEY,
    document_type TEXT,
    document_date TEXT,
    declared_by   TEXT,
    declared_at   TEXT
);
CREATE TABLE IF NOT EXISTS registry_folders (
    folder_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    updated_by  TEXT,
    updated_at  TEXT
);
"""


def _db_path(cfg: Config | None = None) -> Path:
    return (cfg or Config.load()).storage_root / "app.db"


def _conn(cfg: Config | None = None) -> sqlite3.Connection:
    # WAL + a real busy_timeout: this file is shared with api/appdb.py and
    # api/review_votes.py, all writing the same app.db under concurrent
    # requests. See api/appdb.py's _conn() for why that matters on an async
    # route (a bare SQLITE_BUSY wait there blocks the whole event loop).
    # sqlite_util.connect also retries the WAL setup itself against a
    # transient external lock (a cloud-sync client touching the file).
    c = sqlite_util.connect(_db_path(cfg), timeout=30.0)
    c.row_factory = sqlite3.Row
    c.executescript(_DDL)
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Documents: the uploader's declared metadata, projected into SQL.
# The intake sidecar stays the durable per-document home the pipeline reads;
# this table is the SAME declaration projected into SQL so the app can join
# and list relationally. Both are written from one dict in one request, and
# sync_documents_from_sidecars() rebuilds the table from sidecars, so the
# sidecar always wins on disagreement.
# --------------------------------------------------------------------------- #
_DOC_COLS = ("document_type", "document_date", "declared_by", "declared_at")


def upsert_document(doc_id: str, intake: dict,
                    cfg: Config | None = None) -> None:
    """Project one document's declared intake into registry_documents."""
    with _conn(cfg) as c:
        sets = ", ".join(f"{k}=excluded.{k}" for k in _DOC_COLS)
        c.execute(
            f"INSERT INTO registry_documents(doc_id, {', '.join(_DOC_COLS)}) "
            f"VALUES(?,?,?,?,?) ON CONFLICT(doc_id) DO UPDATE SET {sets}",
            (doc_id,) + tuple((intake.get(k) or "").strip() or None
                              for k in _DOC_COLS))


def list_documents(cfg: Config | None = None) -> dict[str, dict]:
    """All declared document rows keyed by doc_id."""
    with _conn(cfg) as c:
        rows = c.execute("SELECT * FROM registry_documents").fetchall()
        return {r["doc_id"]: dict(r) for r in rows}


def sync_documents_from_sidecars(cfg: Config | None = None) -> int:
    """Idempotent backfill: project every raw/*.meta.json intake declaration
    into registry_documents (documents declared before the table existed, or
    a table lost with its DB). Returns rows written."""
    cfg = cfg or Config.load()
    n = 0
    for doc_id, sidecar in _read_sidecars(cfg).items():
        intake = sidecar.get("intake") or {}
        if not intake:
            continue
        upsert_document(doc_id, intake, cfg=cfg)
        n += 1
    return n


# --------------------------------------------------------------------------- #
# Folders: named contract families. A folder NAMES an existing group string
# (the sidecar "group" that scopes the amendment DAG). The folder_id IS the
# group value, so renaming a folder never touches a single document: the
# group mechanism stays untouched underneath and the folder is pure display.
# --------------------------------------------------------------------------- #
_FOLDER_ID_PREFIX = "fld:"


def _read_sidecars(cfg: Config) -> dict[str, dict]:
    """Every raw/*.meta.json sidecar, keyed by doc_id. Unreadable files skip."""
    raw_dir = cfg.storage_root / "raw"
    out: dict[str, dict] = {}
    if not raw_dir.exists():
        return out
    import json
    for p in raw_dir.glob("*.meta.json"):
        try:
            out[p.name[:-len(".meta.json")]] = json.loads(
                p.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            continue
    return out


def sidecar_groups(cfg: Config | None = None) -> dict[str, str]:
    """doc_id to declared family group, from the sidecars (docs without a
    group are absent). The sidecar is the durable home of the grouping, so
    folder membership reads from here, not from the graph copy."""
    cfg = cfg or Config.load()
    return {doc_id: str(sc["group"])
            for doc_id, sc in _read_sidecars(cfg).items() if sc.get("group")}


def mint_folder_id(name: str, at: str | None = None) -> str:
    """A fresh admin-created folder id, safe to use as a sidecar group string:
    'fld:' plus 10 hex chars of sha1(name + timestamp)."""
    import hashlib
    seed = f"{name}{at or _now()}".encode("utf-8")
    return _FOLDER_ID_PREFIX + hashlib.sha1(seed).hexdigest()[:10]


def list_folders(cfg: Config | None = None,
                 include_inactive: bool = False) -> list[dict]:
    with _conn(cfg) as c:
        rows = c.execute(
            "SELECT * FROM registry_folders"
            + ("" if include_inactive else " WHERE active = 1")
            + " ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def get_folder(folder_id: str, cfg: Config | None = None) -> dict | None:
    with _conn(cfg) as c:
        r = c.execute("SELECT * FROM registry_folders WHERE folder_id = ?",
                      (folder_id,)).fetchone()
        return dict(r) if r else None


def upsert_folder(folder_id: str, name: str,
                  *, by: str | None = None, cfg: Config | None = None) -> None:
    with _conn(cfg) as c:
        c.execute(
            "INSERT INTO registry_folders(folder_id, name, active, "
            "updated_by, updated_at) VALUES(?,?,1,?,?) "
            "ON CONFLICT(folder_id) DO UPDATE SET "
            "name=excluded.name, updated_by=excluded.updated_by, "
            "updated_at=excluded.updated_at",
            (folder_id.strip(), name.strip(), by, _now()))


def set_folder_active(folder_id: str, active: bool,
                      *, by: str | None = None,
                      cfg: Config | None = None) -> bool:
    """Retire (active=False) or restore a folder. Soft, never a delete:
    documents keep their group string either way. Returns True on change."""
    with _conn(cfg) as c:
        cur = c.execute(
            "UPDATE registry_folders SET active = ?, updated_by = ?, "
            "updated_at = ? WHERE folder_id = ?",
            (1 if active else 0, by, _now(), folder_id))
        return cur.rowcount > 0


def _default_folder_name(group: str, sidecars: dict[str, dict]) -> str:
    """The display name a scanned group starts with. 'fam:' prefixes are
    machine minting (fam:<parent-doc-id-prefix>), so for those we prefer the
    parent document's own title over the bare hash."""
    name = group[len("fam:"):] if group.startswith("fam:") else group
    if group.startswith("fam:") and _re.fullmatch(r"[0-9a-f]{6,}", name):
        for doc_id, sc in sidecars.items():
            if doc_id.startswith(name):
                title = str(sc.get("title") or "").strip()
                if title:
                    return title
    return name or group


def sync_folders_from_groups(cfg: Config | None = None) -> int:
    """Idempotent backfill: every distinct sidecar group gets a folder row so
    the group becomes nameable. INSERT OR IGNORE only, so an admin-set name
    is never overwritten by a re-sync. Returns rows created."""
    cfg = cfg or Config.load()
    sidecars = _read_sidecars(cfg)
    groups: dict[str, str] = {}
    for _doc_id, sc in sidecars.items():
        g = str(sc.get("group") or "").strip()
        if g and g not in groups:
            groups[g] = _default_folder_name(g, sidecars)
    if not groups:
        return 0
    n = 0
    with _conn(cfg) as c:
        for folder_id, name in groups.items():
            cur = c.execute(
                "INSERT OR IGNORE INTO registry_folders(folder_id, name, "
                "active, updated_by, updated_at) VALUES(?,?,1,?,?)",
                (folder_id, name, "sync:sidecars", _now()))
            n += cur.rowcount
    return n
