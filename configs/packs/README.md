# configs/packs/

The **doctype pack** is the graph-build contract: the single source of truth
that spans the seam between the two halves of the pipeline:

```
PART 1: NORMALISE (per-doc, LLM-assisted)    |  PART 2: GRAPH (cross-doc, deterministic)
raw spans -> typed, comparable facts         |  facts -> dual-label nodes -> derived edges
(fact_types[*].raw_labels / shape / bundling)|  -> canonical hubs -> load -> retrieval
```

Everything left of the seam is interpretive and span-anchored (the LLM's job).
Everything right is deterministic or gated.

```
packs/
|-- _base.yaml             The doctype-INVARIANT engine (inherited by every pack)
`-- <domain>.yaml          extends: _base, declares only what is doctype-specific
```

`pipeline/extraction/pack.py` loads a pack, deep-merges `_base` underneath it
(doctype keys win), compiles it into frozen value objects, and **validates** it.

## `_base.yaml`: the invariant engine

Shared by every doctype, never restated in a doctype pack:

- **The dual-label fact model.** Every extracted fact node carries two labels:
  `:Fact` (the generic spine all doctype-agnostic machinery keys on: evidence
  anchoring, citation, vector recall, cross-doc aggregation) and `:<Type>` (the
  specific typed leaf, generated from a `fact_types` entry's `label`, giving
  indexed, typed lookup). One node, both labels.
- **`structural.node_types`**: the layout/retrieval/evidence/provenance/
  quarantine node tiers (`Document`, `Agreement`, `Section`, `Block`,
  `EvidenceSpan`, `FactMention`, `Proposal`) with their keys and properties.
- **`structural.edges`**: the mechanical loader edges (`HAS_SECTION`,
  `SUPPORTED_BY`, `CITES_BLOCK`, …), pointing at the generic `:Fact` label.
- **`provenance_tiers`**: the edge trust model (`derived` / `asserted_llm` /
  `proposed`) that retrieval and synthesis read to decide how far to trust an
  edge and how to cite it.
- **`retrieval`**, the engine config: the generic fact label, the vector
  indexes, the coarse-recall and citation tiers, and the faithfulness gate.

## A doctype pack (e.g. `commercial_agreement.yaml`)

Declares only the doctype-specific blocks:

| Block | What it defines |
|---|---|
| `fact_types` | Per category: `raw_labels` (harvest-label routing), `shape` + `bundling` (the normalise prompt), and the graph side: `label`, `key`, `properties`, `indexed`, `filterable`, `summary`, plus optional `role_join`, `measure`, `required`. A `null` label means the fact loads as edges, not a node (e.g. `reference`). |
| `dropped_raw_labels` | Raw labels routed to nothing (e.g. `identifier`, `other`, `unknown`). |
| `load_maps` | Declarative normalised-dict → node-property mapping (rename / coalesce / alias / normalized / name / role), so the loader builds node properties from data rather than per-category Python. |
| `identity.hubs` | The cross-document canonical hubs your domain declares, the join keys for cross-doc reasoning. `pivotable` hubs are the grouping axes the aggregation planner may pivot on. |
| `edges.derived` | Deterministic, explainable edges with no LLM: `co_location` (tiered by precision), `role_join` (match a fact's role to a `Party` role), `text_match`. |
| `assertable` | The closed menu of fact-to-fact semantic edges the LLM may assert, each with a required-evidence rule and a domain/range gate. Below the confidence bar they are quarantined as `:Proposal`. |
| `aggregation` | The cross-doc comparison surface: `pivots` × `measures` × reductions. |
| `structural.ui_groups` | How the graph explorer groups and colours node labels, plus a `ui_group` on each structural node type. `GET /graph/legend` serves it, so the frontend holds no copy of any one domain's labels. |

## Validation: the drift gate

`pack.py: compile_mapping` raises `PackError` listing **every** inconsistency at
once (not just the first), so a broken pack fails to compile rather than
loading bad data. It enforces, among others:

- every `indexed` / `filterable` / `summary` / `key` projection is a declared
  property of its label.
- each `raw_label` routes to exactly one category.
- every derived-edge endpoint is a declared label and its provenance is a
  declared tier. `co_location` tiers come from the known set.
- every `role_join.edge` is an actually-declared derived edge.
- every aggregation pivot hub is `pivotable` and every measure label has a
  `measure` block.
- every structural node type names a `ui_group` that the pack declares, so a
  new node type cannot arrive without a place in the legend.

Two `assertable` relations whose endpoints aren't fact labels, a pivot on a
non-pivotable hub, a `measure` field that isn't a property: all surface here,
before any graph write.

## Loading a pack

```python
from pipeline.extraction.pack import load
pack = load("commercial_agreement")   # merge + compile + validate (cached)
```

The compiled `Pack` exposes the accessors the loader and retrieval tools use
(`id_key`, `properties`, `filterable`, `summary_keys`, `pivots`, `measures`, …).
The maps these used to hard-code now come from the pack.
