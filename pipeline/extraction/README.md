# pipeline/extraction/

Per-document extraction: a scanned PDF becomes the typed fact list at
`storage/canonical/<doc>.json`, the seam the graph layer (`pipeline/kb/`) builds
from. A reader recovers pixel-accurate text geometry, then the LLM classifies
structure and extracts **facts only** (never graph edges). All domain knowledge
lives in `configs/`. This package is the runtime.

## Two readers, one seam

`READER` (env, read by `pipeline/config.py`) selects how a PDF becomes the
per-page `pages_md` artifacts. Both paths emit the same shape, so `merge.py`
and everything downstream are identical:

| Mode | Path | Cost |
|---|---|---|
| `rapidocr` (default) | `render.py` → `rapidocr_ocr.py` → `correct_classify.py` | Free local OCR + one LLM correction call per page. Works offline. |
| `cu` | `cu_read.py` (Azure AI Content Understanding, GA API `2025-11-01`, needs `CU_ENDPOINT` + `CU_KEY`) | One paid `analyzeBinary` call per document. Layout, tables and figures come back native, so no vision-correction pass. Raw response cached at `storage/cu_raw/`, billed once per doc. |

## Modules

### Config + introspection
| File | Purpose |
|---|---|
| `loader.py` | Reads the `configs/` tree and composes `ComposedAnalyzer` objects (categories + role taxonomy). Validates `pipeline.yaml` via pydantic. Exposes `load_all`, `get_analyzer`, `render_prompt`. |
| `schemas.py` | Pydantic models for every LLM structured-output response. Doubles as a JSON-Schema generator. |
| `pack.py` | Compiles a Doctype Pack (`configs/packs/`) into runtime objects: fact types, identity hubs, the graph legend. The graph-build contract. |
| `ontology_compile.py` | Compiles extraction-side artifacts from the ontology: `LABEL_TO_CATEGORY`, `CATEGORY_TO_LABEL`, `KNOWN_CATEGORIES`. |
| `prompt_helpers.py` | Pure helpers exposed into the Jinja prompt environment. |
| `lint.py` | CLI: `python -m pipeline.extraction.lint` validates every config in the tree. |
| `show.py` | CLI: `python -m pipeline.extraction.show <analyzer_id>` prints the fully composed analyzer. |

### Pipeline stages (in order)
| File | Step | Purpose |
|---|---|---|
| `render.py` | 1 | PDF → per-page PNGs (pypdfium2) at `configs/pipeline.yaml: render.dpi`. |
| `rapidocr_ocr.py` | 2 | `READER=rapidocr`: PNG → RapidOCR line geometry + verbatim text (ONNX, no system binary). |
| `correct_classify.py` | 3 | `READER=rapidocr`: one LLM call per page to correct OCR misreads + classify blocks. The model never outputs coordinates. Block geometry is the union of line polygons. |
| `cu_read.py` | 1–3 alt | `READER=cu`: one Content Understanding call per document returns markdown + layout with polygon geometry, adapted into the same `pages_md` artifacts `correct_classify.py` produces. |
| `merge.py` | 4 | Per-page JSONs → one ordered `doc.json` (clean, LLM-input) + a `doc_geometry.json` sidecar (block_id → bbox/polygon). Pure. |
| `table_repair.py` | 5 | Vision repair for badly recovered table grids. |
| `understand.py` | 6 | Orchestrator: `outline` → raw-fact source → `categorise` → `normalise` (see below). |
| `structural_units.py` | 6b | Deterministic clause / definition / table segmentation (unit-scoped source, stage 1). |
| `unit_extract.py` | 6b | Per-unit LLM fact extraction (unit-scoped source, stage 2). |
| `categorise.py` | 6c | **Pure Python.** `raw_label → category`, exact-text dedup, regex extraction of Section/Schedule cross-references. Zero LLM cost. |
| `normalise.py` | 6d | One focused LLM call per non-empty category: bundle stubs, normalise values, assign roles. Run concurrently. |
| `text_repair.py` | 6 | Deterministic repair of numeric-confusable OCR errors in fact *values*. |
| `textnorm.py` | (any) | Shared snippet-matching normalisation: neutralises markdown/HTML table syntax and folds unicode so a quoted snippet re-finds against CU markdown pages. Applied to both sides of a comparison, never rewrites stored text. |
| `validate.py` | 7 | Snippet re-find per source + per-category value validator + bbox enrichment. Overwrites `canonical.json` with adjusted confidence. |
| `repair.py` | 8 | Schema-driven field-completeness repair: re-asks the source page for pack-`required` fields the model dropped (faithful, null if genuinely absent). |
| `sections.py` | (load) | Deterministically extracts `Section` records from `doc.json` headings. Used at load. Pure. |
| `embed.py` | (load) | Embeds `EvidenceSpan` + `Section` text (text-embedding-3-large) and writes the vectors onto the same Cosmos items. Search is exact in-RAM cosine in dev (`COSMOS_VECTOR_MODE=client`) or native DiskANN on live Azure (`native`). |
| `canonical_lite.py` | 6–8 alt | **Schema-first mode** (`extraction_mode: schema_first`, env `EXTRACTION_MODE` > CLI `--mode` > yaml, default `legacy`): replaces understand/validate/repair with a fact-less canonical, structure only. The ops-view field extraction then happens in `pipeline/kb/field_llm.py`. |

### Proposal channel + tooling
| File | Purpose |
|---|---|
| `relation_propose.py` | LLM-gated semantic-relation proposal stage. Validates proposed relations against declared domain/range. Valid ones land in `quarantine/<doc>.relation_proposals.json` and are mirrored to `Proposal` items, never asserted as edges. |
| `proposal_review.py` | CLI to review and promote (or reject) those proposals, the explicit human-in-the-loop boundary. |
| `token_meter.py` | Process-wide LLM token accounting for one ingest run. |
| `viz_ocr.py` | CLI debug tool: overlay OCR detections + adapted blocks on the rendered PNGs. |

## The understand step (6)

```
outline      LLM: doctype + section outline
   │
   ├─ raw facts, source selected by configs/pipeline.yaml: fact_source
   │     • whole_doc : one harvest LLM call over the whole document (default)
   │     • unit      : structural_units.py segmentation + per-unit unit_extract.py
   │
categorise   PURE PYTHON: raw_label → category, dedup, regex cross-refs
   │
normalise    LLM: one focused call per non-empty category (bundling + role assign)
   ▼
storage/canonical/<doc>.json
```

Each pass caches on the SHA of its input, so editing one prompt re-fires only that
pass plus everything downstream. Every async stage accepts an injectable `llm_call=`
so the orchestration is unit-tested without the network.

## Per-document cost (rough)

With `READER=rapidocr` the dominant cost is `correct_classify`, **one LLM call per
page**. With `READER=cu` the read is instead one paid Content Understanding call
per document and the per-page correction pass is skipped. On top of the read,
a handful of document-level calls: `outline` (1), the raw-fact source (1 for
`whole_doc`, or N for `unit`), `normalise` (about one per non-empty category), and
`repair` (only for facts missing required fields). `render`, `merge`, `categorise`,
`sections`, and `validate` are free (local CPU / pure Python).

## CLIs

```sh
python -m pipeline.extraction.lint                       # validate configs/
python -m pipeline.extraction.show <domain>              # inspect a composed analyzer

# Per-stage runs are available (python -m pipeline.extraction.<stage> <doc_id>),
# but the normal entry point is the whole-pipeline driver:
python -m pipeline.ingest <doc_id>
```

## Extending

Categories and roles are declared in `configs/analyzers/`. Fact types, identity
hubs, and the graph legend live in `configs/packs/`. Node labels and edge types in
`configs/ontology/`. To add a doctype or a fact type, edit those, not this package.
See `configs/README.md`, `configs/packs/README.md`, and `configs/ontology/README.md`.
