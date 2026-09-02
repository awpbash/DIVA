"""kb/intake.py — declared upload metadata (what the uploader states as fact).

At upload the admin declares things the PDF can never be trusted to carry:
how the
document relates to an existing one (amendment / novation / supersedence).
That declaration is HUMAN input, so it outranks extraction — extraction
becomes a cross-check that flags disagreement instead of deciding identity.

The declaration lives in the document's ``raw/<doc_id>.meta.json`` sidecar
under an ``intake`` key, next to title/source_path/group. The sidecar is the
durable home (it survives graph nukes and rides every rebuild); kb/km.py reads
it when materialising the KM layer.

Sidecar shape::

    {"title": ..., "source_path": ..., "group": ...,
     "intake": {"relation": ...,
                "parent_doc_id": ..., "document_type": ..., "document_date": ...,
                "declared_by": ..., "declared_at": ...}}
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..config import Config
from ..storage import Paths, atomic_write_json

RELATIONS = ("standalone", "amends", "novates", "supersedes")
_LINK_RELATIONS = ("amends", "novates", "supersedes")
def doc_types(doctype: str | None = None) -> tuple[str, ...]:
    """What an uploader may declare a document to BE, from the active domain.

    This was a literal tuple of one domain's vocabulary, so every deployment
    offered its uploaders "Schematic/Drawing" whether or not such a thing
    existed in their world, and had no way to offer what did. Declared under
    `document_family.document_types` in the view instead.
    """
    from .opsview_spec import family_policy
    policy = family_policy(doctype) if doctype else family_policy()
    return policy.document_types


def _read_sidecar(cfg: Config, doc_id: str) -> dict:
    p = Paths(cfg).raw_meta_json(doc_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_intake(cfg: Config, doc_id: str) -> dict:
    """The declared intake dict for a document ({} when nothing was declared)."""
    return _read_sidecar(cfg, doc_id).get("intake") or {}


def load_group(cfg: Config, doc_id: str) -> str | None:
    """The document's declared family from its sidecar (None when ungrouped).
    The sidecar wins over the graph copy — an upload can family a document
    AFTER it was loaded, and the graph only catches up at the next KM build."""
    return _read_sidecar(cfg, doc_id).get("group") or None


def merged_sidecar(existing: dict, *, intake: dict, group: str | None) -> dict:
    """Pure merge: fold a new declaration into an existing sidecar. The
    declaration replaces the previous intake wholesale (it IS the newest human
    statement); ``group`` only ever upgrades a missing family, never clobbers
    one (folder-scanned families from CLI intake stay authoritative)."""
    out = dict(existing)
    out["intake"] = {k: v for k, v in intake.items() if v not in (None, "")}
    if group and not out.get("group"):
        out["group"] = group
    return out


def save_intake(cfg: Config, doc_id: str, *, intake: dict,
                group: str | None = None) -> dict:
    """Merge a declaration into the sidecar on disk and return the result."""
    p = Paths(cfg).raw_meta_json(doc_id)
    merged = merged_sidecar(_read_sidecar(cfg, doc_id), intake=intake, group=group)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(p, merged, indent=None)
    return merged


def link_relation(relation: str | None) -> bool:
    """True when the relation declares an edge to a parent document."""
    return (relation or "").lower() in _LINK_RELATIONS


def family_for_parent(cfg: Config, parent_doc_id: str) -> str:
    """The family a linked upload joins: the parent's declared family, else a
    fresh family keyed on the parent (so base + amendment always share one)."""
    parent = _read_sidecar(cfg, parent_doc_id)
    return parent.get("group") or f"fam:{parent_doc_id[:12]}"


def ensure_group(cfg: Config, doc_id: str, group: str) -> bool:
    """Backfill a family onto a document that has none (the parent of a linked
    upload). Never overwrites an existing family. Returns True if written."""
    p = Paths(cfg).raw_meta_json(doc_id)
    sidecar = _read_sidecar(cfg, doc_id)
    if sidecar.get("group"):
        return False
    sidecar["group"] = group
    sidecar.setdefault("title", doc_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(p, sidecar, indent=None)
    return True


def set_group(cfg: Config, doc_id: str, group: str | None) -> dict:
    """Set or clear the document's family on its sidecar. Unlike ensure_group
    this OVERWRITES: a folder move is an explicit admin decision, the newest
    human statement of where the document belongs. Everything else in the
    sidecar (title, intake, source_path) is preserved. Returns the sidecar."""
    p = Paths(cfg).raw_meta_json(doc_id)
    sidecar = _read_sidecar(cfg, doc_id)
    sidecar["group"] = group or None
    sidecar.setdefault("title", doc_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(p, sidecar, indent=None)
    return sidecar


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
