"""api/routes/ontology.py — CRUD for the ops-view schema (field definitions + sensitivity).

The ops-view (configs/views/<domain>_ops.yaml) is the schema-first extraction TARGET and
the contract the review UI + KB build on. This lets admins EVOLVE it — add / edit /
delete fields (user fields are LLM-extracted from their hint) and set per-category RBAC
sensitivity — without hand-editing YAML.

Edits persist as ROWS in the app database (pipeline/kb/ontology_store.py) merged over
the curated base at load, so several admins can edit concurrently without clobbering a
shared file, and every field edit records who made it. Every write is validated through
the drift gate (compile_with_overlay) BEFORE it lands, so a broken edit never persists.
Mutations are Admin-only (capability enforced server-side from the session).
"""
from __future__ import annotations

import copy

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from pipeline.kb import ontology_store as store
from pipeline.kb.opsview_spec import (
    OpsViewError, compile_with_overlay, current_overlay, effective_sensitivity,
)
from pipeline.kb.opsview_spec import load as load_view

from .. import appdb
from .auth import require_admin

router = APIRouter(prefix="/ontology", tags=["ontology"])

_LEVELS = {"general", "confidential"}
# Types a user may pick for a new field. `value` = a number+unit; `enum` = a closed
# choice; the rest are free text / references.
_USER_TYPES = {"text", "number", "value", "enum", "free_text"}


def _validate_with(mutate) -> None:
    """Apply ``mutate`` to a COPY of the live overlay and run it through the drift
    gate — the store is only written after the prospective schema compiles."""
    prospective = copy.deepcopy(current_overlay())
    mutate(prospective)
    try:
        compile_with_overlay(prospective)
    except OpsViewError as e:
        raise HTTPException(400, f"invalid ontology edit — {e}") from e


# --------------------------------------------------------------------------- #
@router.get("")
def get_ontology(user: dict = Depends(require_admin)) -> dict:
    """The full ontology as the UI needs it: categories, their fields (with who
    last edited each user field), and each category's RBAC sensitivity level.
    Admin-only — the Ontology tab is an admin surface."""
    view = load_view()
    eff = effective_sensitivity()
    edits = store.attribution()
    cats: dict[str, dict] = {}
    for f in view.fields:
        c = cats.setdefault(f.category, {
            "key": f.category,
            "title": view.category_titles.get(f.category, f.category),
            "level": eff["categories"].get(f.category, eff["default_level"]),
            "fields": [],
        })
        who = edits.get(f.full_key) or {}
        c["fields"].append({
            "key": f.key, "full_key": f.full_key, "title": f.title, "type": f.type,
            "values": list(f.values), "hint": f.hint, "sensitivity": f.sensitivity,
            "mechanism": f.mechanism, "multiplicity": f.multiplicity, "origin": f.origin,
            # only user-authored (llm) fields are freely deletable; base fields are the
            # curated contract (editable hint/sensitivity, but not removed casually).
            "user_field": f.mechanism == "llm",
            "updated_by": who.get("updated_by"), "updated_at": who.get("updated_at"),
        })
    return {"categories": list(cats.values()), "default_level": eff["default_level"],
            "can_edit": user.get("role") == "admin"}


class FieldBody(BaseModel):
    category: str
    key: str
    title: str
    type: str = "text"
    hint: str = ""
    values: list[str] | None = None       # for type=enum
    multiplicity: int = 1


@router.post("/field")
def add_field(body: FieldBody, admin: dict = Depends(require_admin)) -> dict:
    """Add a USER field — extracted by the LLM from its hint (mechanism: llm)."""
    if body.type not in _USER_TYPES:
        raise HTTPException(400, f"type must be one of {sorted(_USER_TYPES)}")
    key = body.key.strip().lower().replace(" ", "_").replace("-", "_")
    if not key.isidentifier():
        raise HTTPException(400, "key must be a simple identifier (letters, digits, _)")
    fdef: dict = {"title": body.title, "type": body.type,
                  "multiplicity": max(1, body.multiplicity),
                  "hint": body.hint, "origin": "user",
                  "source": {"mechanism": "llm"}}
    if body.type == "enum":
        vals = [v for v in (body.values or []) if str(v).strip()]
        if not vals:
            raise HTTPException(400, "an enum field needs values")
        if "Not Stated" not in vals:
            vals.append("Not Stated")
        fdef["values"] = vals

    if store.has_field(body.category, key):
        raise HTTPException(409, f"field {body.category}.{key} already exists")

    # The short key has to be unique across the WHOLE schema, not just within
    # its category. Both aggregation paths match rows on `field_key`, the short
    # name, so two categories defining the same key fold their values into one
    # sum or one list with nothing to indicate it happened. The per-category
    # check above would have allowed exactly that.
    from pipeline.kb.opsview_spec import load as load_view
    clash = next((f for f in load_view().fields
                  if f.key == key and f.category != body.category), None)
    if clash is not None:
        raise HTTPException(
            409,
            f"the field key {key!r} is already used by {clash.full_key}. Keys "
            f"must be unique across every category, because aggregation matches "
            f"on the key alone and two fields sharing one would be added "
            f"together. Pick a more specific name.")

    def mutate(ov: dict) -> None:
        cat = ov.setdefault("categories", {}).setdefault(body.category, {"fields": {}})
        cat.setdefault("fields", {})[key] = fdef
        ov["deleted"] = [d for d in (ov.get("deleted") or []) if d != f"{body.category}.{key}"]

    _validate_with(mutate)
    store.upsert_field(body.category, key, fdef, by=admin["email"])
    appdb.log_event(admin["email"], "ontology", "added field",
                    target=f"{body.category}.{key}", detail=body.title)
    return {"ok": True, "full_key": f"{body.category}.{key}", "mechanism": "llm"}


class EditBody(BaseModel):
    title: str | None = None
    hint: str | None = None
    values: list[str] | None = None
    sensitivity: str | None = None        # general | confidential (per-field override)


@router.put("/field/{category}/{key}")
def edit_field(category: str, key: str, body: EditBody,
               admin: dict = Depends(require_admin)) -> dict:
    """Edit a field's title / hint / enum values (partial — merges over the base)."""
    patch: dict = {}
    if body.title is not None:
        patch["title"] = body.title
    if body.hint is not None:
        patch["hint"] = body.hint
    if body.values is not None:
        vals = [v for v in body.values if str(v).strip()]
        if "Not Stated" not in vals:
            vals.append("Not Stated")
        patch["values"] = vals
    if body.sensitivity is not None and body.sensitivity not in _LEVELS:
        raise HTTPException(400, f"sensitivity must be one of {sorted(_LEVELS)}")

    def mutate(ov: dict) -> None:
        if patch:
            cat = ov.setdefault("categories", {}).setdefault(category, {"fields": {}})
            cat.setdefault("fields", {})
            cat["fields"][key] = {**cat["fields"].get(key, {}), **patch}
        if body.sensitivity is not None:
            (ov.setdefault("sensitivity", {}).setdefault("field_overrides", {})
             )[f"{category}.{key}"] = body.sensitivity

    _validate_with(mutate)
    if patch:
        store.upsert_field(category, key, patch, merge=True, by=admin["email"])
    if body.sensitivity is not None:
        store.set_sensitivity(category, body.sensitivity, key, by=admin["email"])
    appdb.log_event(admin["email"], "ontology",
                    "edited field" if patch else "changed field sensitivity",
                    target=f"{category}.{key}",
                    detail=", ".join(sorted(patch))
                    + (f" sensitivity={body.sensitivity}" if body.sensitivity else ""))
    return {"ok": True, "full_key": f"{category}.{key}"}


@router.delete("/field/{category}/{key}")
def delete_field(category: str, key: str, admin: dict = Depends(require_admin)) -> dict:
    """Delete a field. A user (overlay) field is removed outright; a base field is
    tombstoned so the merge drops it (reversible by re-adding)."""
    in_overlay = store.has_field(category, key)

    def mutate(ov: dict) -> None:
        fields = ov.get("categories", {}).get(category, {}).get("fields", {}) or {}
        if key in fields:
            del fields[key]
        else:
            tomb = set(ov.get("deleted") or [])
            tomb.add(f"{category}.{key}")
            ov["deleted"] = sorted(tomb)

    _validate_with(mutate)
    store.delete_field(category, key, base_field=not in_overlay, by=admin["email"])
    appdb.log_event(admin["email"], "ontology", "deleted field",
                    target=f"{category}.{key}")
    return {"ok": True, "deleted": f"{category}.{key}"}


class SensitivityBody(BaseModel):
    category: str
    level: str                             # general | confidential


@router.put("/sensitivity")
def set_sensitivity(body: SensitivityBody, admin: dict = Depends(require_admin)) -> dict:
    """Set a whole review CATEGORY's RBAC level (confidential categories are hidden
    from the Default role in the review, knowledge base and chat)."""
    if body.level not in _LEVELS:
        raise HTTPException(400, f"level must be one of {sorted(_LEVELS)}")

    def mutate(ov: dict) -> None:
        ov.setdefault("sensitivity", {}).setdefault("categories", {})[body.category] = body.level

    _validate_with(mutate)
    store.set_sensitivity(body.category, body.level, by=admin["email"])
    appdb.log_event(admin["email"], "ontology", "changed category sensitivity",
                    target=body.category, detail=body.level)
    return {"ok": True, "category": body.category, "level": body.level}
