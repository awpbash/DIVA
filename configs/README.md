# configs/

The declarative layer. Everything that defines **what** the pipeline extracts
from a document, **how** those values become a knowledge graph, and **which**
node labels and edges are allowed lives here as data, separated from the code
that runs it.

One deployment serves one domain, and a domain is the set of files below that
carry its name.

```
configs/
|-- pipeline.yaml          Global runtime defaults (the active domain, DPI,
|                          concurrency, confidence gates)
|-- analyzers/             EXTRACTION: categories + per-category roles, per doctype
|   |-- _universal/        Base: the category vocabulary, no roles
|   `-- <domain>/          extends _universal, adds the typed role taxonomy
|-- packs/                 GRAPH-BUILD CONTRACT: fact types, identity hubs,
|   |-- _base.yaml         derived/asserted edges, the graph legend
|   `-- <domain>.yaml
|-- ontology/              The declared node-label + edge-type ontology
|   `-- <domain>.yaml           (enforced by a drift-gate test)
|-- views/                 The field schema per domain (<domain>_ops.yaml:
|                          what to capture, and the scored extraction target)
|-- policy/                Access policy: which fact labels are confidential,
|                          and which roles are denied them, per domain
|-- prompts/               LLM prompts, one per file. <name>.<domain>.md
|                          overrides <name>.md
`-- schemas/               JSON meta-schema(s) the loader validates analyzers against
```

## Three configuration layers

The tree spans three independent contracts. Each is loaded and validated by a
different piece of code, and each fails fast on a malformed input.

| Layer | Files | Loaded / validated by | Defines |
|---|---|---|---|
| **Analyzers** | `analyzers/<doctype>/analyzer.yaml` | `pipeline/extraction/loader.py` | The fact **categories** an extraction may emit, and the **roles** to assign within each category. |
| **Packs** | `packs/<doctype>.yaml` + `packs/_base.yaml` | `pipeline/extraction/pack.py` | The doctype "pack": fact types (the normalise → graph seam), identity hubs, derived/asserted edges, and the aggregation surface. |
| **Ontology** | `ontology/<doctype>.yaml` | `tests/extraction/test_ontology_compile.py` (drift gate) | The closed set of graph **node labels** and **edge types**, with their domain/range. |

### 1. `analyzers/`: what to extract

An analyzer declares two things and nothing else:

* **`categories`**: the fact categories the extraction is allowed to assign.
* **`roles`**: a per-category role taxonomy that labels a fact with its
  domain role (e.g. a `money` fact becomes the `deposit`, and an `organization`
  becomes the `supplier`).

`_universal/analyzer.yaml` is the base: it declares the category vocabulary and
no roles. A doctype analyzer does `extends: _universal` to inherit that
vocabulary and layers its own `roles` block on top. `loader.py` composes the
two into a single `ComposedAnalyzer` (categories + `roles_by_category`), caching
it by a stable hash of all inputs. See `analyzers/README.md`.

### 2. `packs/`: how facts become a graph

A doctype **pack** is the single source of truth that spans the seam between
the two halves of the system:

```
PART 1: NORMALISE (per-doc, LLM-assisted)    |  PART 2: GRAPH (cross-doc, deterministic)
raw spans -> typed, comparable facts         |  facts -> dual-label nodes -> derived edges
(fact_types[*].raw_labels / shape / bundling)|  -> canonical hubs -> load -> retrieval
```

`packs/_base.yaml` holds the doctype-invariant engine: the dual-label fact
model (`Fact` spine + a typed leaf label), the layout/evidence/provenance
node tiers, the structural edges, the edge trust model (`provenance_tiers`),
and the retrieval engine config. `packs/<domain>.yaml` does
`extends: _base` and declares only what is specific to that domain: its
`fact_types`, `load_maps`, identity `hubs`, derived `edges`, the `assertable`
relation menu, and the `aggregation` surface.

`pack.py` deep-merges `_base` under the doctype pack (doctype keys win), then
**validates** the result. A filterable property that isn't declared, an edge
endpoint that isn't a label, a pivot whose hub isn't pivotable, all fail to
compile, and the error lists every problem at once. This is the build's drift
gate. See `packs/README.md`.

### 3. `ontology/`: the allowed graph shape

`ontology/<domain>.yaml` is the declared inventory of every graph
**node label** and **relationship type**, with each edge's domain/range and
provenance tier. A drift-gate test (`tests/extraction/test_ontology_compile.py`)
fails the build if code writes a label or edge that isn't declared here, or
declares one that no loader writes. This is what keeps the LLM, or a code
change, from introducing non-canonical edge names. See `ontology/README.md`.

## `pipeline.yaml`: global runtime defaults

Runtime knobs shared across doctypes (analyzers may override the confidence
thresholds). Secrets (API keys, DB endpoints) live in `.env` / `pipeline/config.py`,
never here.

| Key | Meaning |
|---|---|
| `render.dpi`, `render.image_format` | Page rasterization for OCR. |
| `vision.*` | Per-page concurrency and retry budget for the page-audit pass. |
| `domain` | The domain this deployment serves. Overridden by `VERBATIM_DOMAIN`. Omit it and a checkout shipping exactly one pack autodetects. |
| `vision.prompt` | Prompt name for the page pass, resolved to `prompts/<name>.md`. |
| `load.confidence.verified` / `tentative` | Confidence gates: at or above `verified` a fact loads as verified, between the two it loads tentative and is queued for review, below `tentative` it is logged only. |
| `snippet.max_edit_ratio`, `snippet.case_sensitive` | Tolerance for the on-page snippet re-find, which is how a value is checked against the clause it claims to come from. |

`loader.py` validates `pipeline.yaml` against a pydantic model at startup and
refuses to run if `tentative > verified`.

## Adding a new doctype

Four files, not three. See [docs/domains.md](../docs/domains.md) for the full
walkthrough, which this list only summarises.

1. `analyzers/<domain>/analyzer.yaml`: `extends: _universal`, list the enabled
   `categories` and the per-category `roles`.
2. `ontology/<domain>.yaml`: declare every node label and edge the loader will
   write, plus the `identity` block (canonical roles, legal suffixes, generic
   tokens).
3. `packs/<domain>.yaml`: `extends: _base`, declare the `fact_types`,
   `edges.derived`, `assertable`, identity `hubs`, and `aggregation`.
4. `views/<domain>_ops.yaml`: the field schema, including the
   `document_family.field_roles` map that the amendment chain reads.

Then `python -m scripts.setup --check`, then the test suite. The loader, pack
compiler, ontology drift gate and per-domain contract tests all validate the
new domain, and `setup --check` reports the two misconfigurations that
otherwise fail silently: an unmapped chain role, and an access-policy class
naming a label the pack does not declare.
