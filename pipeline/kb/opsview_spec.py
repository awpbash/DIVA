"""kb/opsview_spec.py — load + validate the ops-view spec.

The ops-view (`configs/views/<doctype>_ops.yaml`) is the SCORED extraction target: a
projection of a doctype's extracted facts onto an operational field schema.
This module compiles that YAML into a frozen ``OpsView`` and validates
it against the doctype pack, so the mapping can NEVER reference a fact label or
property that doesn't exist and no enum can be left open. It is the drift gate
for the core schema contract.

Consumers:
  * ``eval.extraction.score`` — reads the mapping to know WHERE each field's value
    lives in the graph, then grades it against gold.
  * (later) a deterministic load-time derivation pass — same mapping, writes the
    values back as tags / ``OpsField`` nodes.

Pure and deterministic: no store, no network. Validation collects EVERY problem and
raises once (mirrors ``pipeline/extraction/pack.py``), so a spec edit is one-shot.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from pipeline.extraction.pack import Pack, load as load_pack
from pipeline.ontology import DEFAULT_DOCTYPE

_VIEWS_DIR = Path(__file__).resolve().parents[2] / "configs" / "views"


def view_path(doctype: str = DEFAULT_DOCTYPE) -> Path:
    """The view file for one doctype. One naming rule, no registry."""
    return _VIEWS_DIR / f"{doctype}_ops.yaml"


# The default doctype's view. Kept as a module constant because the legacy
# overlay path is defined relative to it, and because most callers have no
# doctype in scope and mean this one.
_VIEW_PATH = view_path(DEFAULT_DOCTYPE)

# Field value-types and the source mechanisms, and which pack property each
# mechanism reads (validated against the pack's declared properties).
_TYPES = {"reference", "value", "equipment", "enum", "presence_enum",
          "free_text", "number", "text"}
_MECHANISMS = {"value", "equipment", "enum", "presence_enum",
               "free_text", "external", "party", "recital", "llm"}
# `grounding` was a tenth mechanism: look the value up in an external reference
# register (a customer or asset master list) that the deployment loaded
# alongside its documents. Those registers were one firm's master data rather
# than part of the framework and were retired, which left this validating
# against a pack field that no longer exists. `external` is what remains for a
# value that lives outside the document: it is declared, and it abstains.
# `llm` — the mechanism for USER-DEFINED fields added through the ontology CRUD UI:
# the schema-first extractor fills it from the document per the field's hint/title,
# with evidence. LLM-native like `recital` (no generic graph fact backs it, so the
# derive path returns empty and validation adds no pack requirement). This is what
# makes "add a field" just work — describe it, the model extracts it.
# `recital` — filled by the schema-first LLM from the document's opening (recitals /
# "WHEREAS" / dating clauses): document type, ordinal, effective date, and the
# supersedence edges it DECLARES ("…the Agreement dated X, amended by…"). LLM-native:
# no generic graph fact backs it, so the derive path returns empty and validation
# adds no pack requirement (like `external`, but the LLM DOES fill it).
_NOT_STATED = "Not Stated"


# Party roles the extractor assigns (the recital/signature block resolver). A
# `party` field names one of these; validated so a typo can't silently match
# nothing. Read from config PER DOCTYPE rather than hardcoded, so a domain whose
# parties are licensor/licensee is not forced into a supplier/customer world.
#
# Derivation, entirely from what the domain already declares:
#   1. the pack's role-scoped identity hub names the label that carries party
#      roles (`CanonicalParty` from `Party` in both shipped domains),
#   2. every fact category mapping to that label is party-bearing
#      (`organization` and `person`),
#   3. the analyzer's declared roles for those categories are the vocabulary.
@lru_cache(maxsize=None)
def party_roles(doctype: str = DEFAULT_DOCTYPE) -> frozenset[str]:
    from pipeline.extraction.loader import get_analyzer

    pack = load_pack(doctype)
    party_labels = {h.from_label for h in pack.hubs.values() if h.role_scoped}
    if not party_labels:
        return frozenset()
    analyzer = get_analyzer(doctype)
    roles: set[str] = set()
    for cat, ft in pack.fact_types.items():
        if ft.label in party_labels:
            roles |= set(analyzer.roles_for(cat))
    return frozenset(roles)


@dataclass(frozen=True)
class FamilyPolicy:
    """How a domain's document family behaves: which fields supersede, which
    document types are links in the amendment chain, and which relationship
    words declare which graph edge.

    This was three hardcoded constants in `km.py`, which meant the supersedence
    walk (the piece the "current value" guarantee rests on) only understood one
    domain's vocabulary. It is declared per view instead.
    """

    #: View categories EXCLUDED from the per-field supersedence walk. These
    #: describe the document itself or the stable identity of the parties, not
    #: an evolving contract term, so a later document must not overwrite them.
    non_superseding_categories: frozenset[str]
    #: Document types that state values but are never a link in the amendment
    #: chain, so they are never a base an amendment resolves to. Normalised.
    ancillary_doc_types: frozenset[str]
    #: relationship word (lowercased) -> the graph edge it declares.
    relation_edges: dict[str, str]
    #: What an uploader may declare a document to BE, in menu order. Domain
    #: vocabulary: a lease register and a contract register do not share one.
    #: Falls back to `DEFAULT_DOC_TYPES` when a view declares none.
    document_types: tuple[str, ...]
    #: Structural ROLE -> the field key this domain uses for it, within the
    #: document_relationships category. The chain walk asks for a role and the
    #: domain names the field, because "the date this document is dated" is a
    #: universal role while `agreement_date` and `document_date` are two
    #: domains' words for it. Falls back to `DEFAULT_FIELD_ROLES`.
    field_roles: dict[str, str]


# Used when a view declares no `document_family.document_types`. Deliberately
# generic: every corpus has a base document, things that change it, and
# correspondence about it.
DEFAULT_DOC_TYPES: tuple[str, ...] = (
    "Agreement", "Amendment", "Assignment", "Letter", "Notice", "Other",
)

# The six structural roles the amendment chain reads, mapped to the field names
# a domain gets if it declares no `field_roles`. These defaults are the first
# domain's names, kept only so an existing view stays valid without an edit.
# A new domain almost certainly names its fields differently and MUST declare
# the mapping: an unmapped role reads a field that does not exist, and a chain
# walk that finds nothing looks exactly like a corpus with no amendments.
DEFAULT_FIELD_ROLES: dict[str, str] = {
    "document_type": "document_type",
    "ordinal": "supplemental_ordinal",
    "relationship_type": "relationship_type",
    "document_date": "document_date",
    "amends_dated": "amends_agreement_dated",
    "effective_date": "amendment_effective_date",
}
FIELD_ROLES = tuple(DEFAULT_FIELD_ROLES)


@lru_cache(maxsize=None)
def family_policy(doctype: str = DEFAULT_DOCTYPE) -> FamilyPolicy:
    """Read the document-family policy straight from the view file.

    Deliberately reads the FILE and not `load()`: this is structural policy, not
    a user-editable field, so it must not depend on the ontology overlay or on
    the app database being reachable.
    """
    doc = yaml.safe_load(view_path(doctype).read_text(encoding="utf-8")) or {}
    fam = doc.get("document_family") or {}
    non_superseding = {
        key for key, cat in (doc.get("categories") or {}).items()
        if (cat or {}).get("supersedes") is False
    }
    return FamilyPolicy(
        non_superseding_categories=frozenset(non_superseding),
        ancillary_doc_types=frozenset(
            " ".join(str(t).split()).strip().lower()
            for t in (fam.get("ancillary_types") or ())
        ),
        relation_edges={
            str(k).strip().lower(): str(v)
            for k, v in (fam.get("relations") or {}).items()
        },
        document_types=tuple(str(t) for t in (fam.get("document_types") or ()))
                       or DEFAULT_DOC_TYPES,
        field_roles=_field_roles(doc, fam, doctype),
    )


#: The category the amendment chain lives in. A view that omits it declares a
#: corpus with no document family, which is legal: the chain walk finds nothing
#: and every document stands alone.
REL_CATEGORY = "document_relationships"


def _field_roles(doc: dict, fam: dict, doctype: str) -> dict[str, str]:
    """Resolve `document_family.field_roles`, falling back per role.

    Validated against the view's own fields rather than trusted, because the
    failure mode is silent: a role pointing at a field that does not exist
    returns None from every lookup, and a chain that never orders looks
    identical to a corpus that has no amendments in it.
    """
    declared = {str(k): str(v) for k, v in (fam.get("field_roles") or {}).items()}
    unknown = set(declared) - set(DEFAULT_FIELD_ROLES)
    if unknown:
        raise OpsViewError(
            f"{doctype}: document_family.field_roles names unknown role(s) "
            f"{sorted(unknown)}. Known roles: {sorted(DEFAULT_FIELD_ROLES)}")

    roles = {**DEFAULT_FIELD_ROLES, **declared}
    cats = doc.get("categories") or {}
    if REL_CATEGORY not in cats:
        return roles

    have = set(((cats.get(REL_CATEGORY) or {}).get("fields") or {}))
    # Only DECLARED roles are enforced. An undeclared role falling back to a
    # name this domain does not use is the case the defaults exist to tolerate,
    # and `missing_field_roles` reports it rather than failing the build.
    missing = {r: f for r, f in declared.items() if f not in have}
    if missing:
        raise OpsViewError(
            f"{doctype}: document_family.field_roles points at field(s) that "
            f"{REL_CATEGORY} does not declare: "
            + ", ".join(f"{r} -> {f!r}" for r, f in sorted(missing.items())))
    return roles


def rel_full_key(role: str, doctype: str = DEFAULT_DOCTYPE) -> str:
    """The stored `document_relationships.<field>` key for a structural role.

    Callers that query opsfield rows by `full_key` need the domain's own field
    name, so they go through here rather than writing one domain's spelling
    into a query and quietly matching nothing everywhere else.
    """
    return f"{REL_CATEGORY}.{family_policy(doctype).field_roles[role]}"


def missing_field_roles(doctype: str = DEFAULT_DOCTYPE) -> dict[str, str]:
    """Roles whose resolved field is absent from the view's own
    document_relationships category. Empty is the healthy answer.

    This is the check that would have caught a whole domain silently losing its
    amendment chain, so it is surfaced to the setup command rather than left
    for someone to notice a wrong answer months later.
    """
    doc = yaml.safe_load(view_path(doctype).read_text(encoding="utf-8")) or {}
    cats = doc.get("categories") or {}
    if REL_CATEGORY not in cats:
        return {}
    have = set(((cats.get(REL_CATEGORY) or {}).get("fields") or {}))
    return {r: f for r, f in family_policy(doctype).field_roles.items()
            if f not in have}


class OpsViewError(ValueError):
    """Raised when the ops-view spec is inconsistent with the pack. Lists every
    problem found, not just the first."""


@dataclass(frozen=True)
class OpsField:
    category: str
    key: str
    title: str
    type: str
    multiplicity: int
    values: tuple[str, ...]      # resolved enum vocab ('' -> empty)
    source: dict                 # raw mapping: {mechanism, label/labels, match, ...}
    origin: str                  # 'view' | 'rbac_matrix'
    sensitivity: str = "general" # 'general' | 'confidential' — RBAC visibility level
    hint: str = ""               # optional extraction guidance for the schema-first LLM

    @property
    def mechanism(self) -> str:
        return str(self.source.get("mechanism") or "")

    @property
    def full_key(self) -> str:
        return f"{self.category}.{self.key}"


@dataclass(frozen=True)
class OpsView:
    version: int
    extends_pack: str
    fields: tuple[OpsField, ...]
    category_titles: dict

    def by_category(self) -> dict[str, list[OpsField]]:
        out: dict[str, list[OpsField]] = {}
        for f in self.fields:
            out.setdefault(f.category, []).append(f)
        return out

    @property
    def categories(self) -> tuple[str, ...]:
        """Category names in the order the schema declares them. This is the
        display order for every surface that groups fields, so a domain author
        controls it by ordering their own YAML."""
        return tuple(self.by_category())

    def field(self, full_key: str) -> OpsField | None:
        for f in self.fields:
            if f.full_key == full_key or f.key == full_key:
                return f
        return None


def _resolve_values(fld: dict, enums: dict) -> tuple[str, ...]:
    if "values_ref" in fld:
        return tuple(enums.get(fld["values_ref"]) or [])
    return tuple(fld.get("values") or [])


def compile_view(doc: dict, pack: Pack) -> OpsView:
    """Validate a parsed ops-view dict against ``pack``; return a frozen OpsView
    or raise ``OpsViewError`` listing all problems."""
    problems: list[str] = []
    enums = doc.get("enums") or {}
    sens = doc.get("sensitivity") or {}
    sens_default = str(sens.get("default_level") or "general")
    sens_cats = sens.get("categories") or {}
    sens_over = sens.get("field_overrides") or {}
    labels = pack.fact_labels
    cats = doc.get("categories") or {}
    fields: list[OpsField] = []
    cat_titles: dict[str, str] = {}

    def has_prop(label: str, prop: str) -> bool:
        return label in labels and prop in pack.properties(label)

    for cat_key, cat in cats.items():
        cat_titles[cat_key] = (cat or {}).get("title") or cat_key
        for fkey, fld in ((cat or {}).get("fields") or {}).items():
            where = f"{cat_key}.{fkey}"
            ftype = fld.get("type")
            if ftype not in _TYPES:
                problems.append(f"{where}: unknown field type {ftype!r}")
            mult = fld.get("multiplicity")
            if not isinstance(mult, int) or mult < 1:
                problems.append(f"{where}: multiplicity must be a positive int, got {mult!r}")
            values = _resolve_values(fld, enums)
            src = fld.get("source") or {}
            mech = src.get("mechanism")
            if mech not in _MECHANISMS:
                problems.append(f"{where}: unknown source mechanism {mech!r}")

            # Closed-enum guarantee.
            if ftype in ("enum", "presence_enum"):
                if not values:
                    problems.append(f"{where}: enum field has no values (values_ref/values)")
                elif _NOT_STATED not in values:
                    problems.append(f"{where}: enum values must include {_NOT_STATED!r}")

            # Per-mechanism structural validation against the pack.
            match = src.get("match") or {}
            if mech == "value":
                lbl = src.get("label")
                if lbl not in labels:
                    problems.append(f"{where}: value source label {lbl!r} is not a pack fact label")
                elif not has_prop(lbl, "normalized_parameter"):
                    problems.append(f"{where}: {lbl} has no normalized_parameter to match on")
                if not (match.get("parameter_any") or match.get("parameter_prefix")
                        or match.get("concept_any")):
                    problems.append(f"{where}: value source needs match.parameter_any, parameter_prefix or concept_any")
            elif mech == "enum":
                lbl = src.get("label")
                role_field = src.get("role_field", "actor_role")
                if lbl not in labels:
                    problems.append(f"{where}: enum source label {lbl!r} is not a pack fact label")
                else:
                    if not has_prop(lbl, "canonical_action"):
                        problems.append(f"{where}: {lbl} has no canonical_action")
                    if not has_prop(lbl, role_field):
                        problems.append(f"{where}: {lbl} has no {role_field}")
                if not any(k in match for k in ("action_any", "action_prefix")):
                    problems.append(f"{where}: enum source needs match.action_any or action_prefix")
                for v in (src.get("role_map") or {}).values():
                    if values and v not in values:
                        problems.append(f"{where}: role_map value {v!r} not in field values {list(values)}")
            elif mech == "equipment":
                if "Equipment" not in labels:
                    problems.append(f"{where}: Equipment is not a pack fact label")
                else:
                    fieldname = match.get("field")
                    if fieldname and not has_prop("Equipment", fieldname):
                        problems.append(f"{where}: Equipment has no property {fieldname!r}")
                    if match.get("type_any") and not has_prop("Equipment", "equipment_type"):
                        problems.append(f"{where}: Equipment has no equipment_type")
                    if not (fieldname or match.get("type_any")):
                        problems.append(f"{where}: equipment source needs match.field or match.type_any")
            elif mech in ("presence_enum", "free_text"):
                for lbl in (src.get("labels") or []):
                    if lbl not in labels:
                        problems.append(f"{where}: {mech} references unknown label {lbl!r}")
                if not (src.get("labels")):
                    problems.append(f"{where}: {mech} source needs a non-empty labels list")
            elif mech == "party":
                role = str(src.get("role") or "").lower()
                allowed = party_roles(pack.doctype)
                if not role:
                    problems.append(f"{where}: party source needs a `role`")
                elif role not in allowed:
                    problems.append(
                        f"{where}: party role {role!r} is not declared by the "
                        f"{pack.doctype} analyzer {sorted(allowed)}")
            # 'external' needs no source validation (out-of-document by design).

            level = sens_over.get(f"{cat_key}.{fkey}") or sens_cats.get(cat_key) or sens_default
            fields.append(OpsField(
                category=cat_key, key=fkey, title=fld.get("title") or fkey,
                type=str(ftype), multiplicity=int(mult) if isinstance(mult, int) else 0,
                values=values, source=src, origin=str(fld.get("origin") or "view"),
                sensitivity=str(level), hint=str(fld.get("hint") or ""),
            ))

    if problems:
        raise OpsViewError(
            f"ops-view failed validation ({len(problems)} problem(s)):\n  - "
            + "\n  - ".join(problems))

    return OpsView(
        version=int(doc.get("version") or 0),
        extends_pack=str(doc.get("extends_pack") or DEFAULT_DOCTYPE),
        fields=tuple(fields), category_titles=cat_titles,
    )


# User edits (ontology CRUD) live in an OVERLAY merged over the curated base view,
# so the hand-authored YAML (and its comments) stays intact and every change is
# reversible. The overlay lives in the app DATABASE (storage/app.db — one row per
# edit, with who/when) so several admins can edit concurrently without clobbering
# each other. An overlay YAML sitting next to the view is imported once and then
# renamed to `.migrated`, which is how an install that predates the database
# overlay carries its edits across.
#
# The name is derived from the active view rather than written out. It used to
# be one deployment's filename, which put a customer's name into every copy of
# this repository to serve a migration that only that deployment could ever run.
_OVERLAY_PATH = _VIEW_PATH.with_suffix("").with_suffix(".overlay.yaml")


def current_overlay() -> dict:
    """The live edit overlay from the store (migrating a legacy YAML first)."""
    from . import ontology_store
    if _OVERLAY_PATH.exists():
        ontology_store.migrate_yaml(_OVERLAY_PATH)
    return ontology_store.overlay_dict()


def apply_overlay(doc: dict, overlay: dict) -> dict:
    """Merge a user overlay over the base view dict: add/replace fields, delete
    fields by "category.key", and override per-category / per-field sensitivity."""
    cats = doc.setdefault("categories", {})
    for ckey, cval in (overlay.get("categories") or {}).items():
        base = cats.setdefault(ckey, {"title": (cval or {}).get("title") or ckey, "fields": {}})
        base.setdefault("fields", {})
        if (cval or {}).get("title"):
            base["title"] = cval["title"]
        for fkey, fdef in ((cval or {}).get("fields") or {}).items():
            existing = base["fields"].get(fkey)               # shallow-merge over a
            if isinstance(existing, dict) and isinstance(fdef, dict):
                base["fields"][fkey] = {**existing, **fdef}   # base field (partial edit
            else:                                             # keeps source/type), else
                base["fields"][fkey] = fdef                   # add the new field
    for dotted in (overlay.get("deleted") or []):
        ck, _, fk = str(dotted).partition(".")
        if ck in cats and fk in (cats[ck].get("fields") or {}):
            del cats[ck]["fields"][fk]
    sens_ov = overlay.get("sensitivity") or {}
    if sens_ov:
        sens = doc.setdefault("sensitivity", {})
        sens.setdefault("categories", {}).update(sens_ov.get("categories") or {})
        sens.setdefault("field_overrides", {}).update(sens_ov.get("field_overrides") or {})
        if sens_ov.get("default_level"):
            sens["default_level"] = sens_ov["default_level"]
    return doc


def _base_doc_and_pack(doctype: str = DEFAULT_DOCTYPE):
    doc = yaml.safe_load(view_path(doctype).read_text(encoding="utf-8")) or {}
    return doc, load_pack(doc.get("extends_pack") or doctype)


def effective_sensitivity(doctype: str = DEFAULT_DOCTYPE) -> dict:
    """The merged sensitivity config (base + overlay): the panel shows per-category
    level so the sensitivity controls reflect the review categories directly."""
    doc, _ = _base_doc_and_pack(doctype)
    doc = apply_overlay(doc, current_overlay())
    s = doc.get("sensitivity") or {}
    return {"default_level": str(s.get("default_level") or "general"),
            "categories": s.get("categories") or {},
            "field_overrides": s.get("field_overrides") or {}}


def compile_with_overlay(overlay: dict, doctype: str = DEFAULT_DOCTYPE) -> OpsView:
    """Compile the base view WITH a prospective overlay applied — used by the CRUD
    API to VALIDATE an edit before persisting it (raises OpsViewError if it breaks)."""
    doc, pack = _base_doc_and_pack(doctype)
    return compile_view(apply_overlay(doc, overlay), pack)


def load(path: Path | None = None, doctype: str = DEFAULT_DOCTYPE) -> OpsView:
    """Read + compile + validate the ops-view spec, with any user overlay merged in.

    The user overlay (`ontology_store`, the admin CRUD screen) is scoped to the
    DEFAULT doctype's view: it is one table of field edits with no doctype
    column. So it is applied only when loading that view. Another doctype's view
    loads from its file alone, which is correct today and becomes a real
    per-doctype overlay when the ontology store grows a doctype column.
    """
    p = path or view_path(doctype)
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if path is None and doctype == DEFAULT_DOCTYPE:
        doc = apply_overlay(doc, current_overlay())
    pack = load_pack(doc.get("extends_pack") or doctype)
    return compile_view(doc, pack)


def order_categories(present) -> list[str]:
    """Order the categories a surface actually holds by the active schema's own
    declaration order.

    A category the schema does not declare is kept, sorted, at the end. Never
    dropped: a hardcoded list of one domain's category names used to sit here,
    and on any other domain it silently hid every field outside the one name the
    two schemas happened to share.
    """
    have = set(present)
    declared = load().categories
    return ([c for c in declared if c in have]
            + sorted(have - set(declared)))
