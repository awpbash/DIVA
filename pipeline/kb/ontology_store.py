"""kb/ontology_store.py — the SQL-backed ontology overlay (multi-admin editing).

The ontology has two layers:
  * the BASE schema — `configs/views/<domain>_ops.yaml`, hand-curated, in git, drift-gated.
    It stays a file: the engineering contract belongs in version control.
  * the EDIT overlay — what admins change through the app (added fields, edits,
    tombstones, sensitivity). This used to be one YAML file, which meant two admins
    saving at once clobbered each other's work and nothing recorded who changed what.
    It now lives as ROWS in the app database (storage/app.db — same file that holds
    accounts/sessions), so concurrent edits to different fields don't collide and every
    row carries `updated_by` / `updated_at`.

One row per edit, keyed (kind, category, field_key):
  kind='field'        payload = the field definition / partial patch (JSON)
  kind='delete'       a tombstone hiding a base field (payload unused)
  kind='sens_cat'     payload = {"level": "confidential"}  (field_key = '')
  kind='sens_field'   payload = {"level": ...}             (per-field override)

``overlay_dict()`` compiles the rows into EXACTLY the dict shape
``opsview_spec.apply_overlay`` already consumes — the validation gate
(compile_with_overlay) and every consumer stay unchanged. A legacy
A legacy overlay YAML beside the active view is imported once and renamed
``.migrated``.

stdlib sqlite3 only — importable from both the pipeline and the API without
new dependencies; sqlite's own locking handles concurrent writers.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config

_KINDS = ("field", "delete", "sens_cat", "sens_field")

_DDL = """
CREATE TABLE IF NOT EXISTS ontology_edits (
    kind       TEXT NOT NULL,
    category   TEXT NOT NULL,
    field_key  TEXT NOT NULL DEFAULT '',
    payload    TEXT,
    updated_by TEXT,
    updated_at TEXT,
    PRIMARY KEY (kind, category, field_key)
);
"""


def _db_path(cfg: Config | None = None) -> Path:
    return (cfg or Config.load()).storage_root / "app.db"


def _conn(cfg: Config | None = None) -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(cfg))
    c.row_factory = sqlite3.Row
    c.execute(_DDL)
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Write path (the ontology CRUD API calls these; each is one transaction)
# --------------------------------------------------------------------------- #
def upsert_field(category: str, key: str, fdef: dict, *, merge: bool = False,
                 by: str | None = None, cfg: Config | None = None) -> None:
    """Add or edit a field. ``merge=True`` shallow-merges over an existing row's
    payload (a partial edit keeps the parts it doesn't touch)."""
    with _conn(cfg) as c:
        if merge:
            row = c.execute(
                "SELECT payload FROM ontology_edits WHERE kind='field' AND category=? AND field_key=?",
                (category, key)).fetchone()
            if row and row["payload"]:
                try:
                    fdef = {**json.loads(row["payload"]), **fdef}
                except json.JSONDecodeError:
                    pass
        c.execute(
            "INSERT INTO ontology_edits(kind, category, field_key, payload, updated_by, updated_at) "
            "VALUES('field',?,?,?,?,?) "
            "ON CONFLICT(kind, category, field_key) DO UPDATE SET "
            "payload=excluded.payload, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (category, key, json.dumps(fdef, ensure_ascii=False), by, _now()))
        # Re-adding a previously deleted field drops its tombstone.
        c.execute("DELETE FROM ontology_edits WHERE kind='delete' AND category=? AND field_key=?",
                  (category, key))


def has_field(category: str, key: str, cfg: Config | None = None) -> bool:
    with _conn(cfg) as c:
        return c.execute(
            "SELECT 1 FROM ontology_edits WHERE kind='field' AND category=? AND field_key=?",
            (category, key)).fetchone() is not None


def delete_field(category: str, key: str, *, base_field: bool,
                 by: str | None = None, cfg: Config | None = None) -> None:
    """Delete: a user (overlay) field is removed outright; a base field gets a
    tombstone row so the merge drops it (reversible by re-adding)."""
    with _conn(cfg) as c:
        c.execute("DELETE FROM ontology_edits WHERE kind='field' AND category=? AND field_key=?",
                  (category, key))
        if base_field:
            c.execute(
                "INSERT INTO ontology_edits(kind, category, field_key, payload, updated_by, updated_at) "
                "VALUES('delete',?,?,NULL,?,?) "
                "ON CONFLICT(kind, category, field_key) DO UPDATE SET "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (category, key, by, _now()))


def set_sensitivity(category: str, level: str, field_key: str | None = None,
                    *, by: str | None = None, cfg: Config | None = None) -> None:
    kind = "sens_field" if field_key else "sens_cat"
    with _conn(cfg) as c:
        c.execute(
            "INSERT INTO ontology_edits(kind, category, field_key, payload, updated_by, updated_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(kind, category, field_key) DO UPDATE SET "
            "payload=excluded.payload, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (kind, category, field_key or "", json.dumps({"level": level}), by, _now()))


# --------------------------------------------------------------------------- #
# Read path (opsview_spec + the API)
# --------------------------------------------------------------------------- #
def overlay_dict(cfg: Config | None = None) -> dict:
    """Compile the rows into the overlay dict ``apply_overlay`` consumes:
    {categories: {cat: {fields: {key: def}}}, deleted: [...], sensitivity: {...}}."""
    if not _db_path(cfg).exists():
        return {}
    out: dict = {}
    with _conn(cfg) as c:
        for r in c.execute("SELECT * FROM ontology_edits"):
            kind = r["kind"]
            if kind == "field":
                try:
                    fdef = json.loads(r["payload"] or "{}")
                except json.JSONDecodeError:
                    continue
                (out.setdefault("categories", {})
                    .setdefault(r["category"], {"fields": {}})
                    .setdefault("fields", {}))[r["field_key"]] = fdef
            elif kind == "delete":
                out.setdefault("deleted", []).append(f"{r['category']}.{r['field_key']}")
            elif kind in ("sens_cat", "sens_field"):
                try:
                    level = (json.loads(r["payload"] or "{}") or {}).get("level")
                except json.JSONDecodeError:
                    continue
                if not level:
                    continue
                sens = out.setdefault("sensitivity", {})
                if kind == "sens_cat":
                    sens.setdefault("categories", {})[r["category"]] = level
                else:
                    sens.setdefault("field_overrides", {})[
                        f"{r['category']}.{r['field_key']}"] = level
    if "deleted" in out:
        out["deleted"] = sorted(set(out["deleted"]))
    return out


def attribution(cfg: Config | None = None) -> dict[str, dict]:
    """{ 'category.key': {updated_by, updated_at} } for field edits — shown in the
    ontology UI so admins can see who last touched a field."""
    if not _db_path(cfg).exists():
        return {}
    with _conn(cfg) as c:
        return {f"{r['category']}.{r['field_key']}":
                {"updated_by": r["updated_by"], "updated_at": r["updated_at"]}
                for r in c.execute("SELECT * FROM ontology_edits WHERE kind='field'")}


# --------------------------------------------------------------------------- #
# One-time migration from the legacy YAML overlay
# --------------------------------------------------------------------------- #
def migrate_yaml(overlay_path: Path, cfg: Config | None = None) -> int:
    """Import a legacy overlay YAML into the store, then rename it to
    ``.migrated`` so it never double-applies. Returns rows imported (0 = nothing
    to migrate). Idempotent — a missing file is a no-op."""
    if not overlay_path.exists():
        return 0
    import yaml
    try:
        doc = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return 0
    n = 0
    for ckey, cval in (doc.get("categories") or {}).items():
        for fkey, fdef in ((cval or {}).get("fields") or {}).items():
            upsert_field(ckey, fkey, fdef if isinstance(fdef, dict) else {},
                         by="migrated:yaml", cfg=cfg)
            n += 1
    for dotted in (doc.get("deleted") or []):
        ck, _, fk = str(dotted).partition(".")
        delete_field(ck, fk, base_field=True, by="migrated:yaml", cfg=cfg)
        n += 1
    sens = doc.get("sensitivity") or {}
    for cat, level in (sens.get("categories") or {}).items():
        set_sensitivity(cat, str(level), by="migrated:yaml", cfg=cfg)
        n += 1
    for dotted, level in (sens.get("field_overrides") or {}).items():
        ck, _, fk = str(dotted).partition(".")
        set_sensitivity(ck, str(level), fk, by="migrated:yaml", cfg=cfg)
        n += 1
    overlay_path.rename(overlay_path.with_suffix(".yaml.migrated"))
    return n
