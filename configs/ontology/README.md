# configs/ontology/

The declared knowledge-graph schema: the single source of truth for which
**node labels** and **relationship types** are allowed, and how extraction maps
onto them.

```
ontology/
`-- <domain>.yaml           One ontology per document family
```

Everything a loader writes must be declared here, and everything declared here
must be written by a loader. A drift-gate test
(`tests/extraction/test_ontology_compile.py`) fails the build in either
direction: adding a label or edge in code without declaring it (or declaring
one no loader writes) breaks the build. This is what stops the LLM, or a code
change, from introducing a non-canonical node label or edge name.

## Structure

| Section | Contents |
|---|---|
| `layers` | The node labels grouped by tier (document, layout, fact, evidence, provenance, identity, quarantine). |
| `node_types` | Per label: `key`, a `description`, and the `properties`. Property lists are documentation, validated lightly (the key and `doc_id` must be present). |
| `relationships` | Per edge: `from` / `to` domain and range, a `provenance` value, and a `description`. Some edges declare explicit `pairs` (allowed endpoint combinations) instead of the full Cartesian product, or `edge_properties`. |
| `extraction` | The extraction contract: per category, the `label`, `raw_labels`, `shape_doc`, and `bundling_rule`. Compiled by `pipeline/extraction/ontology_compile.py` into the harvest-label routing, the category → node-label map, and the per-category normalise prompt. |
| `identity` | The identity policy: which roles mint a canonical party, and the `legal_suffixes` used to normalise names into a canonical key. |
| `relation_proposals` | The LLM proposal channel: candidate relations written as `Proposal` items for human review, not asserted as edges directly. |

## Provenance values

Each relationship declares how it gets into the graph:

| `provenance` | Meaning |
|---|---|
| `loader` | Written directly from extraction artifacts. |
| `derived:in_graph` | Deterministic derivation computed in Python over loaded records (`pipeline/kb/derive.py`), idempotent. |
| `derived:text_match` | Deterministic string matching over evidence text. |
| `asserted:review` | A review-approved semantic relation promoted from a proposal. |

An edge marked `assertable: true` carries an `llm_description` and may be
offered to the LLM as part of the closed fact-to-fact relation menu (with
per-assertion evidence and a domain/range gate at load). The absence of the
flag means the edge is never offered to the LLM.

## Accessors

`pipeline/ontology.py` exposes the read helpers query-side and load-side code
use (`load_ontology`, `node_labels`, `relationship_types`, `node_key`,
`relationship_endpoints`, `category_label_map`, `raw_label_map`,
`assertable_relations`), so the declaration and the code never drift apart.

## Relationship to `packs/`

The pack (`configs/packs/`) and the ontology overlap by design: the pack is the
compiled build engine (fact types, hubs, derived/asserted edges),
and the ontology is the closed allow-list of labels and edges the graph may
contain. The pack compiler validates the pack is internally consistent, and the
ontology drift gate validates that what code writes matches the declared
schema. A new doctype needs both.

## Planned

`AMENDS` / `SUPERSEDES` / `NOVATES` between agreements are now implemented and
declared in the YAML: `pipeline/kb/timeline.py` derives them deterministically
from declared recital text, gated to `Proposal` when ambiguous. What remains
planned lives only as comments in the YAML so the consistency test ignores it:
asserted-relation provenance (`asserted:llm`) carrying per-assertion evidence
from a relate pass.
