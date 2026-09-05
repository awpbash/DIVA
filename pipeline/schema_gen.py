"""pipeline/schema_gen.py — generate a brand-new domain's four config files
from a plain field list, for the setup wizard's "build from scratch" path.

See docs/domains.md for what each file means. This module exists because of
one deliberate split: content vs. structure.

CONTENT (the field list: name, type, hint) is whatever the user typed or an
AI drafted — every generated field lands in the ops-view as
``source: {mechanism: llm}``, which is filled directly by the model reading
the whole document against the field's own hint (`pipeline/kb/field_llm.py`)
and needs NOTHING declared in the analyzer, ontology or pack. That is what
makes a from-scratch domain with no toggles at all work with an almost-bare
skeleton of those three files — confirmed by reading
`pipeline/kb/opsview_spec.py:compile_view`, where `llm` (like `recital` and
`external`) has no per-mechanism structural validation at all.

STRUCTURE (the toggles) is never freely generated. Each is a FIXED template,
parameterised only by things the user already typed or picked:

  * confidential  — per-field, into `sensitivity.field_overrides`. Free: no
    file but the ops-view changes.
  * totals        — turned out to need NOTHING here at all. `type: value` on
    a field is sufficient regardless of mechanism: `pipeline/kb/km.py`'s
    `parse_numbers()` runs on every field's stored value STRING and feeds
    `aggregate_ops_fields` directly. Resolves the plan's own [VERIFY] in
    section 3.5. There is no `totals` toggle here — picking type "value" in
    the field editor already IS it.
  * amendment_chain — a fixed `document_relationships` category (5 exact
    field keys, matching `docs/domains.md`'s worked example almost verbatim)
    plus the `document_family` policy block, parameterised only by the
    user's own document-type vocabulary.
  * party_matching — the genuinely hard one. A fixed organization fact type
    + CanonicalParty identity hub, mirroring the shipped
    `commercial_agreement` domain's own (see configs/packs/commercial_agreement.yaml),
    parameterised only by which of the user's OWN fields play a party-name
    role (a field titled "Landlord" becomes role `landlord`). The AI's part
    ends at suggesting which fields look like org/person names — this
    module never generates that YAML freely, it fills in a template.

Two validations gate every generated domain before it is ever written to
its real config path: `pipeline.extraction.pack.load()` and
`pipeline.kb.opsview_spec.load()` — the exact two `scripts/setup.py:
check_configs()` already runs. `configs/ontology/<domain>.yaml` is written
for consistency and for the OFFLINE contract test suite
(`tests/extraction/test_ontology_contract.py` and friends), but note it is
NOT read by either live validator above (confirmed by reading both modules'
imports) — so it is generated carefully, but the module docstring says so
rather than pretending it is validated here too.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

_REPO_ROOT_NAME = "configs"  # sanity marker only; real paths built by callers

# Legal-entity suffixes stripped when matching org names into one canonical
# key. Genuinely domain-neutral (the same list `commercial_agreement` ships),
# not something worth re-asking the user for.
_LEGAL_SUFFIXES = ["inc", "incorporated", "corp", "corporation", "co", "llc",
                   "llp", "lp", "ltd", "limited", "plc", "gmbh", "sa", "nv",
                   "bv", "pte"]

# The five fixed field keys the amendment-chain toggle writes — matching
# docs/domains.md's worked example, so an operator who later reads that doc
# recognises the shape immediately.
AMENDMENT_FIELD_KEYS = ("document_type", "amendment_ordinal", "amends_document",
                        "agreement_date", "effective_date")

DEFAULT_DOCUMENT_TYPES = ("Agreement", "Amendment", "Amended and Restated",
                          "Assignment", "Side Letter", "Notice", "Other")

_USER_TYPES = {"text", "number", "value", "enum", "free_text"}


class SchemaGenError(ValueError):
    """A field list or toggle parameter is invalid — caught before any file
    is written, so this never surfaces as a confusing pack/ops-view error
    about a file the user never saw."""


def slugify(text: str) -> str:
    """The exact key-cleaning rule `api/routes/ontology.py:add_field` already
    uses (lower, spaces/dashes -> underscore) — one definition of "how a
    title becomes a key", so a from-scratch field and a runtime-added field
    turn identical titles into identical keys."""
    key = str(text or "").strip().lower().replace(" ", "_").replace("-", "_")
    key = re.sub(r"[^a-z0-9_]", "", key)
    return key


def slug_domain(text: str) -> str:
    """A domain identifier: `configs/schemas/analyzer.schema.json` requires
    `^_?[a-z][a-z0-9_]*$`, and the folder name must equal it exactly."""
    s = slugify(text)
    s = re.sub(r"^[0-9_]+", "", s)
    return s or "custom_domain"


@dataclass
class DraftField:
    key: str
    title: str
    type: str = "text"
    hint: str = ""
    values: tuple[str, ...] = ()
    category: str = "details"
    multiplicity: int = 1
    confidential: bool = False


@dataclass
class AmendmentChainToggle:
    enabled: bool = False
    document_types: tuple[str, ...] = DEFAULT_DOCUMENT_TYPES


@dataclass
class PartyMatchingToggle:
    enabled: bool = False
    # Keys of fields (already in the field list) that name a party — each
    # becomes one role, slugified from that field's OWN title.
    field_keys: tuple[str, ...] = ()


@dataclass
class GeneratedDomain:
    domain: str
    analyzer: dict
    ontology: dict
    pack: dict
    ops_view: dict


# --------------------------------------------------------------------------- #
# Field-list validation — shared by every toggle combination
# --------------------------------------------------------------------------- #
def _validate_fields(fields: list[DraftField]) -> None:
    if not fields:
        raise SchemaGenError("add at least one field before generating a schema")
    seen_keys: set[str] = set()
    for f in fields:
        if f.type not in _USER_TYPES:
            raise SchemaGenError(f"{f.key}: type must be one of {sorted(_USER_TYPES)}")
        if not f.key or not f.key.isidentifier():
            raise SchemaGenError(f"{f.key!r}: key must be a simple identifier")
        if f.key in seen_keys:
            raise SchemaGenError(f"{f.key}: duplicate field key")
        seen_keys.add(f.key)
        if f.type == "enum" and not f.values:
            raise SchemaGenError(f"{f.key}: an enum field needs at least one value")
    for reserved in AMENDMENT_FIELD_KEYS:
        if reserved in seen_keys:
            raise SchemaGenError(
                f"{reserved!r} is a reserved field key (the amendment-chain "
                f"toggle uses it) — rename your field")


# --------------------------------------------------------------------------- #
# Ontology boilerplate — the structural tier every domain needs, since
# configs/ontology/<domain>.yaml has no `extends` (unlike the analyzer/pack).
# Domain-neutral: identical for every domain, so this is infrastructure, not
# "content" the drift-gate guard against domain-naming-in-code cares about.
# --------------------------------------------------------------------------- #
def _structural_ontology() -> dict:
    return {
        "node_types": {
            "Document": {
                "key": "doc_id",
                "description": "One ingested PDF.",
                "properties": ["doc_id", "doctype", "analyzer_id", "total_pages",
                               "n_facts", "extraction_version"],
            },
            "Agreement": {
                "key": "agreement_id",
                "description": "The document's own top-level record (currently 1:1 with Document).",
                "properties": ["agreement_id", "doc_id", "agreement_type", "title",
                               "document_date", "effective_date", "end_date"],
            },
            "Section": {
                "key": "section_id",
                "description": "Numbered heading-delimited region of the document.",
                "properties": ["section_id", "doc_id", "section_num", "title", "kind",
                               "order", "page_start", "page_end",
                               "bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1",
                               "embed_text", "embedding"],
            },
            "Block": {
                "key": "block_id",
                "description": "Physical layout block (paragraph / table / heading) with page geometry.",
                "properties": ["block_id", "doc_id", "page_no", "kind", "text",
                               "section_path", "bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1",
                               "grid_json", "embed_text", "embedding"],
            },
            "EvidenceSpan": {
                "key": "evidence_id",
                "description": "Citation atom — verbatim snippet + page geometry backing a fact.",
                "properties": ["evidence_id", "doc_id", "agreement_id", "raw_fact_id",
                               "page_no", "section_id", "text_span", "snippet_ok",
                               "evidence_type", "block_ids", "bbox_x0", "bbox_y0",
                               "bbox_x1", "bbox_y1", "rects_json", "row_context",
                               "embed_text", "embedding"],
            },
            "FactMention": {
                "key": "mention_id",
                "description": "Provenance floor — the raw harvested span before categorise/normalise.",
                "properties": ["mention_id", "doc_id", "agreement_id", "raw_fact_id",
                               "raw_label", "page_no", "section_id", "text_span",
                               "block_ids", "surfaced", "bbox_x0", "bbox_y0",
                               "bbox_x1", "bbox_y1", "rects_json"],
            },
            "Proposal": {
                "key": "proposal_id",
                "description": "Quarantine for unknown categories and sub-threshold relations.",
                "properties": ["proposal_id", "doc_id", "kind", "category", "reason",
                               "payload_json", "status", "relation", "source_node_id",
                               "target_node_id", "source_label", "target_label",
                               "confidence", "rationale", "evidence_id"],
            },
        },
        "relationships": {
            "CONTAINS_AGREEMENT": {"from": ["Document"], "to": ["Agreement"], "provenance": "loader"},
            "HAS_SECTION": {"from": ["Agreement"], "to": ["Section"], "provenance": "loader"},
            "HAS_BLOCK": {"from": ["Section"], "to": ["Block"], "provenance": "loader"},
            "NEXT": {"from": ["Block"], "to": ["Block"], "provenance": "loader"},
            "CITES_BLOCK": {"from": ["EvidenceSpan", "FactMention"], "to": ["Block"], "provenance": "loader"},
            "IN_SECTION": {"from": ["EvidenceSpan"], "to": ["Section"], "provenance": "loader"},
            "HAS_PROPOSAL": {"from": ["Document"], "to": ["Proposal"], "provenance": "loader"},
        },
    }


def _party_extraction_block() -> dict:
    """The `organization` extraction-contract entry, generalised wording from
    `commercial_agreement.yaml`'s (no "agreement"/"licence" nouns)."""
    return {
        "label": "Party",
        "raw_labels": ["name-or-org", "name_or_org", "organization", "company"],
        "shape_doc": '{"name": "<full legal name>", "notice_address": "<address or null>"}',
        "bundling_rule": ("Bundle stubs that refer to the SAME organization across "
                          "pages (name + notice block). Different organizations "
                          "stay separate."),
    }


def _party_fact_type() -> dict:
    return {
        "label": "Party",
        "key": "party_id",
        "raw_labels": ["name-or-org", "name_or_org", "organization", "company"],
        "shape": '{"name": "<full legal name>", "notice_address": "<address or null>"}',
        "bundling": ("Bundle stubs that refer to the SAME organization across pages "
                     "(name + notice block). Different organizations stay separate."),
        "properties": ["party_id", "doc_id", "agreement_id", "name", "normalized_name",
                       "role", "entity_type", "address", "confidence"],
        "indexed": ["role", "normalized_name"],
        "filterable": ["entity_type", "role", "normalized_name", "name"],
        "summary": ["name", "role"],
    }


# --------------------------------------------------------------------------- #
# The four files
# --------------------------------------------------------------------------- #
def _build_analyzer(domain: str, party: PartyMatchingToggle) -> dict:
    doc: dict = {
        "id": domain,
        "version": 1.0,
        "extends": "_universal",
        # Permissive on purpose: a from-scratch, single-domain deployment
        # never needs to auto-classify AGAINST other domains (there is only
        # ever one active), so a broad match is correct, not sloppy.
        "classify_when": {"any": ["true"]},
        "categories": ["organization"],
    }
    if party.enabled:
        doc["roles"] = {"organization": list(party.field_keys)}
    return doc


def _build_ontology(domain: str, party: PartyMatchingToggle) -> dict:
    struct = _structural_ontology()
    layers = {
        "document": ["Document", "Agreement"],
        "layout": ["Section", "Block"],
        "fact": [],
        "provenance": ["FactMention"],
        "evidence": ["EvidenceSpan"],
        "identity": [],
        "quarantine": ["Proposal"],
    }
    node_types = dict(struct["node_types"])
    relationships = dict(struct["relationships"])
    extraction_categories: dict = {}
    identity: dict = {}

    if party.enabled:
        layers["fact"] = ["Party"]
        layers["identity"] = ["CanonicalParty"]
        node_types["Party"] = {
            "key": "party_id",
            "description": "An organization or individual named in the document.",
            "properties": ["party_id", "doc_id", "agreement_id", "name",
                           "normalized_name", "role", "entity_type", "address",
                           "confidence"],
        }
        node_types["CanonicalParty"] = {
            "key": "canonical_party_id",
            "description": "Cross-document identity hub for a party.",
            "properties": ["canonical_party_id", "normalized_name", "display_name", "roles"],
        }
        relationships["HAS_FACT"] = {"from": ["Agreement"], "to": ["Party"], "provenance": "loader"}
        relationships["SUPPORTED_BY"] = {"from": ["Party"], "to": ["EvidenceSpan"], "provenance": "loader"}
        relationships["HAS_MENTION"] = {"from": ["Party"], "to": ["FactMention"], "provenance": "loader"}
        relationships["RESOLVES_TO"] = {
            "from": ["Party"], "to": ["CanonicalParty"], "provenance": "derived:in_graph",
        }
        extraction_categories["organization"] = _party_extraction_block()
        identity = {
            "canonical_roles": list(party.field_keys),
            "legal_suffixes": list(_LEGAL_SUFFIXES),
            "generic_tokens": [],
        }

    doc = {
        "doctype": domain,
        "version": 1,
        "layers": layers,
        "node_types": node_types,
        "relationships": relationships,
        "extraction": {"dropped_raw_labels": ["identifier", "other", "unknown", ""],
                       "categories": extraction_categories},
    }
    if identity:
        doc["identity"] = identity
    return doc


def _build_pack(domain: str, party: PartyMatchingToggle) -> dict:
    doc: dict = {"doctype": domain, "version": 1, "extends": "_base",
                 "fact_types": {}, "dropped_raw_labels": ["identifier", "other", "unknown", ""]}
    if party.enabled:
        doc["fact_types"]["organization"] = _party_fact_type()
        doc["load_maps"] = {
            "organization": {
                "coalesce": {"name": ["n:name", "$value", "lit:"], "entity_type": ["lit:organization"]},
                "normalized": {"normalized_name": "name"},
            },
        }
        doc["identity"] = {
            "hubs": {
                "CanonicalParty": {
                    "from": "Party", "key": "normalized_name",
                    "mint_when": {"role_in": list(party.field_keys)},
                    "normalize": "legal_suffix", "edge": "RESOLVES_TO",
                    "pivotable": True, "role_scoped": True,
                },
            },
            "legal_suffixes": list(_LEGAL_SUFFIXES),
        }
        # No structural.ui_groups override needed: `fact_types[*].label` and
        # `identity.hubs` names join the "fact" / "identity" legend groups
        # automatically (see configs/analyzers/_universal/analyzer.yaml's
        # comment on this) — only a NEW structural.node_types entry (not a
        # fact type) would need one declared.
    return doc


def _field_source(f: DraftField, party: PartyMatchingToggle) -> dict:
    if party.enabled and f.key in party.field_keys:
        return {"mechanism": "party", "role": f.key}
    return {"mechanism": "llm"}


def _build_ops_view(domain: str, fields: list[DraftField],
                    amendment: AmendmentChainToggle,
                    party: PartyMatchingToggle) -> dict:
    categories: dict = {}
    sens_overrides: dict = {}

    for f in fields:
        cat = categories.setdefault(f.category, {"title": f.category.replace("_", " ").title(), "fields": {}})
        fdef: dict = {
            "title": f.title, "type": f.type, "multiplicity": max(1, f.multiplicity),
            "hint": f.hint, "source": _field_source(f, party),
        }
        if f.type == "enum":
            vals = list(f.values)
            if "Not Stated" not in vals:
                vals.append("Not Stated")
            fdef["values"] = vals
        cat["fields"][f.key] = fdef
        if f.confidential:
            sens_overrides[f"{f.category}.{f.key}"] = "confidential"

    doc: dict = {
        "view": f"{domain}_ops", "extends_pack": domain, "version": 1,
        "categories": categories,
    }
    if sens_overrides:
        doc["sensitivity"] = {"default_level": "general", "field_overrides": sens_overrides}

    if amendment.enabled:
        rel_fields = {
            "document_type": {
                "title": "Document Type", "type": "enum", "multiplicity": 1,
                "hint": "What kind of document this is, read from the title and opening clause.",
                "values": [*amendment.document_types, "Not Stated"],
                "source": {"mechanism": "llm"},
            },
            "amendment_ordinal": {
                "title": "Amendment Ordinal", "type": "text", "multiplicity": 1,
                "hint": "One word only: First / Second / Third, or 'None' if this is the base document.",
                "source": {"mechanism": "llm"},
            },
            "amends_document": {
                "title": "Amends Document", "type": "text", "multiplicity": 1,
                "hint": "The earlier document this one varies, as named in its opening clause.",
                "source": {"mechanism": "llm"},
            },
            "agreement_date": {
                "title": "Document Date", "type": "text", "multiplicity": 1,
                "hint": "The date this document is dated, from its opening clause.",
                "source": {"mechanism": "llm"},
            },
            "effective_date": {
                "title": "Effective Date", "type": "text", "multiplicity": 1,
                "hint": "The date this document takes effect, if stated separately from its date.",
                "source": {"mechanism": "llm"},
            },
        }
        doc["categories"]["document_relationships"] = {
            "title": "Document Relationships", "supersedes": False, "fields": rel_fields,
        }
        doc["document_family"] = {
            "document_types": list(amendment.document_types),
            "relations": {"amendment": "AMENDS", "amended and restated": "SUPERSEDES",
                         "assignment": "NOVATES"},
            "field_roles": {
                "document_type": "document_type", "ordinal": "amendment_ordinal",
                "relationship_type": "document_type", "document_date": "agreement_date",
                "amends_dated": "amends_document", "effective_date": "effective_date",
            },
            "ancillary_types": ["exhibit", "side letter", "ancillary"],
        }

    return doc


def generate(domain: str, fields: list[DraftField], *,
            amendment: AmendmentChainToggle | None = None,
            party: PartyMatchingToggle | None = None) -> GeneratedDomain:
    """Assemble the four files. Raises `SchemaGenError` on a bad field list
    or toggle parameter — always BEFORE any file is written; the caller
    (api/routes/setup.py) still runs the real validators
    (`pack.load`/`opsview_spec.load`) against the assembled result before
    committing it anywhere, since this function cannot see everything those
    do (e.g. a party role colliding with something the field-key checks
    above don't know about)."""
    amendment = amendment or AmendmentChainToggle()
    party = party or PartyMatchingToggle()
    domain = slug_domain(domain)
    _validate_fields(fields)
    if party.enabled:
        unknown = set(party.field_keys) - {f.key for f in fields}
        if unknown:
            raise SchemaGenError(f"party-matching names field(s) not in the field list: {sorted(unknown)}")
        if not party.field_keys:
            raise SchemaGenError("party-matching is on but no fields were marked as party names")

    return GeneratedDomain(
        domain=domain,
        analyzer=_build_analyzer(domain, party),
        ontology=_build_ontology(domain, party),
        pack=_build_pack(domain, party),
        ops_view=_build_ops_view(domain, fields, amendment, party),
    )


def dump_yaml(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


# --------------------------------------------------------------------------- #
# AI drafting — CONTENT only (see the module docstring). One real model call;
# the caller (api/routes/setup.py:draft_schema) is what actually spends
# money, this function is just the request/response shape. Never touches a
# file: the draft is edited in the browser and only becomes real YAML
# through `generate()` above, once the user (not the model) is done with it.
# --------------------------------------------------------------------------- #
_DRAFT_SCHEMA = {
    "name": "schema_draft",
    "strict": True,
    "schema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "key": {"type": "string"},
                        "title": {"type": "string"},
                        "type": {"type": "string",
                                "enum": ["text", "number", "value", "enum", "free_text"]},
                        "hint": {"type": "string"},
                        "category": {"type": "string"},
                        "values": {"type": "array", "items": {"type": "string"}},
                        "confidential": {"type": "boolean"},
                    },
                    "required": ["key", "title", "type", "hint", "category",
                                "values", "confidential"],
                },
            },
        },
        "required": ["fields"],
    },
}


async def draft_fields(description: str) -> list[DraftField]:
    """One model call: a plain-language description -> a starting field
    list. Never raises on a malformed model response for an individual
    field (skips it); raises only if the call itself fails, same contract
    as `scripts/setup.py:test_model_key` — the caller decides how to show
    that to the user."""
    import json

    from .config import Config, make_async_openai
    from .extraction import render_prompt

    cfg = Config.load()
    client = make_async_openai(cfg)
    system = render_prompt("schema_draft").rstrip("\n")
    user = (f"## DESCRIPTION OF THE DOCUMENTS\n{description.strip()}\n\n"
           "Return JSON {\"fields\": [...]} per the schema.")
    resp = await client.chat.completions.create(
        model=cfg.text_model,
        messages=[{"role": "system", "content": system},
                 {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": _DRAFT_SCHEMA},
        max_completion_tokens=4000,
    )
    data = json.loads(resp.choices[0].message.content or "{}")

    out: list[DraftField] = []
    used_keys: set[str] = set()
    for raw in data.get("fields") or []:
        key = slugify(raw.get("key") or raw.get("title") or "")
        if not key or not key.isidentifier() or key in used_keys:
            continue
        ftype = raw.get("type")
        if ftype not in _USER_TYPES:
            continue
        used_keys.add(key)
        out.append(DraftField(
            key=key, title=str(raw.get("title") or key).strip() or key,
            type=ftype, hint=str(raw.get("hint") or "").strip(),
            values=tuple(str(v) for v in (raw.get("values") or []) if str(v).strip()),
            category=slugify(raw.get("category") or "details") or "details",
            confidential=bool(raw.get("confidential")),
        ))
    return out
