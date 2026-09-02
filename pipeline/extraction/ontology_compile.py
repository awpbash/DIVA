"""ontology_compile.py — compile extraction-side artifacts from the ontology.

R3-D1 (see docs/PIPELINE_OVERVIEW.md §5): the ontology YAML is the single
source for what extraction produces. This module turns the declared
`extraction:` section into the objects the pipeline consumes:

  * ``SPECS``              per-category normalise prompt guidance
                           (formerly category_specs.py, now YAML-backed)
  * ``LABEL_TO_CATEGORY``  harvest raw_label -> category routing
                           (consumed by categorise.py)
  * ``CATEGORY_TO_LABEL``  category -> graph node label
                           (consumed by kb/load.py)
  * ``KNOWN_CATEGORIES``   everything extraction may produce, including
                           edge-only categories — the loader's quarantine
                           gate (facts outside this set become Proposals)

Cache semantics: normalise.py hashes the rendered prompt into its
per-category cache key, so editing a shape_doc / bundling_rule in the YAML
re-fires exactly that category's LLM call on the next ingest — nothing
more, nothing less. Schema edits bust precisely the right caches.

Adding a new category = one YAML entry (raw_labels, label, shape_doc,
bundling_rule) + a node_types declaration if it loads as a node. No code.

Per-doctype: ``tables(doctype)`` is the real API and is cached per doctype.
The four module-level names below are the DEFAULT doctype's tables, kept so
existing callers (and the `categorise` re-export) keep working unchanged; any
call site that knows its doctype should ask ``tables()`` for it instead.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from .. import ontology as _ontology

DEFAULT_DOCTYPE = _ontology.DEFAULT_DOCTYPE


@dataclass(frozen=True)
class CategorySpec:
    """Per-category guidance for the normalise pass (prompt-rendered)."""

    category: str
    # JSON-y description of the ``normalised`` dict shape, rendered into
    # the LLM prompt verbatim.
    shape_doc: str
    # Short paragraph explaining what to merge inside this category.
    bundling_rule: str


@dataclass(frozen=True)
class OntologyTables:
    """One doctype's compiled extraction tables."""

    doctype: str
    specs: dict[str, CategorySpec]
    label_to_category: dict[str, str | None]
    category_to_label: dict[str, str]
    known_categories: frozenset[str]


@lru_cache(maxsize=None)
def tables(doctype: str = DEFAULT_DOCTYPE) -> OntologyTables:
    """Compile (and cache) the extraction tables for one doctype."""
    ont = _ontology.load_ontology(doctype)
    cats = _ontology.extraction_categories(ont)
    specs = {
        cat: CategorySpec(
            category=cat,
            shape_doc=str(spec["shape_doc"]),
            bundling_rule=str(spec["bundling_rule"]),
        )
        for cat, spec in cats.items()
    }
    return OntologyTables(
        doctype=doctype,
        specs=specs,
        label_to_category=_ontology.raw_label_map(ont),
        category_to_label=_ontology.category_label_map(ont),
        known_categories=_ontology.known_categories(ont),
    )


@lru_cache(maxsize=None)
def tables_for_analyzer(analyzer_id: str) -> OntologyTables:
    """Tables for the ontology belonging to an ANALYZER id.

    The analyzer id is the config identity (the domain's own name,
    `commercial_agreement`), which is what names an ontology file. It is NOT the
    `doctype` string threaded through the extraction passes: that one is the
    document's classified type as the outline model phrased it ("service
    agreement"), a prompt variable, and it names no file.

    `_universal` is the fallback analyzer used when no doctype matched, and it
    has no ontology of its own, so it resolves to the default doctype's tables.
    """
    if not (_ontology.ONTOLOGY_DIR / f"{analyzer_id}.yaml").exists():
        return tables(DEFAULT_DOCTYPE)
    return tables(analyzer_id)


# Default-doctype views, for callers that have no doctype in scope. Skipped
# when no domain is configured yet (DEFAULT_DOCTYPE == "", see
# pipeline/ontology.py): compiling would open configs/ontology/.yaml and
# crash every module that merely imports this one, before the setup wizard
# ever gets a chance to run. Real use always calls tables() with an actual
# doctype once setup finishes; these module-level names exist only for
# legacy callers that have no doctype in scope, so empty fallbacks are
# correct until then.
DOCTYPE = DEFAULT_DOCTYPE
if DEFAULT_DOCTYPE:
    _DEFAULT = tables(DEFAULT_DOCTYPE)
    SPECS = _DEFAULT.specs
    LABEL_TO_CATEGORY = _DEFAULT.label_to_category
    CATEGORY_TO_LABEL = _DEFAULT.category_to_label
    KNOWN_CATEGORIES = _DEFAULT.known_categories
else:
    _DEFAULT = None
    SPECS = {}
    LABEL_TO_CATEGORY = {}
    CATEGORY_TO_LABEL = {}
    KNOWN_CATEGORIES = frozenset()
