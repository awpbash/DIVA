# Pipeline: one PDF in, a knowledge base out

> Scope: one scanned document through eleven stages to a schema-compliant
> knowledge base in Azure Cosmos DB. The orchestrator is
> [`pipeline/ingest.py`](../pipeline/ingest.py). Retrieval is covered separately
> in [`retrieval.md`](retrieval.md).

Two invariants frame everything:

1. **Fully cached, content-addressed.** `doc_id = sha256(pdf)[:16]`. Every stage
   caches on a hash of its inputs, so re-ingesting the same PDF is a free no-op
   and editing one stage only re-fires what depends on it. `--force` bypasses.
2. **Retrieve coarse, cite fine.** The LLM only ever sees compact text
   (`doc.json`). Pixel-precise geometry rides alongside in `doc_geometry.json`,
   keyed by `block_id`. Only the read stages and table_repair ever touch the
   page image.

**Document reader** (env `READER`, both extraction modes): `rapidocr` (default,
free local OCR plus a vision-LLM cleanup pass) or `cu` (Azure AI Content
Understanding, GA API 2025-11-01, one paid analyze call per document,
`CU_ENDPOINT` + `CU_KEY`, implemented in `pipeline/extraction/cu_read.py`).
Both writers land the same per-page markdown in `pages_md/`, so every stage
downstream of the read is identical whichever reader ran.

**Extraction mode** (`configs/pipeline.yaml: extraction_mode`, env
`EXTRACTION_MODE` > CLI `--mode` > yaml): `legacy` (default, stages 5 to 7 below)
or `schema_first` (skips the open-vocab chain: `canonical_lite.py` writes a
fact-less canonical and load is structure-only). In either mode the ops-view field
extractor (`pipeline/kb/field_llm.py`, evidence-anchored, writes
`storage/fields/`) runs as its own step feeding the review workflow. It fills
the document-body fields of the active domain's field schema
(`configs/views/<domain>_ops.yaml`), and `scripts.build_km` materialises verified
fields into the KM layer after load.

---

## 01 · At a glance

Order is exactly as `ingest.py` runs it. IMG = reads the page image, LLM = calls
a model, DET = deterministic (free). Stage 2 is the reader switch: one row runs,
not both.

| # | Stage | Module | What it does | Mode | Model × calls/doc |
|---|---|---|---|---|---|
| 1 | render | `extraction/render.py` | PDF to page PNGs | DET | none |
| 2 | read (`READER=rapidocr`, default) | `extraction/rapidocr_ocr.py` + `extraction/correct_classify.py` | local OCR (text + boxes per line), then a vision LLM cleans the text and labels blocks, every edit guarded against the raw OCR | IMG LLM | vision × ~1/page |
| 2 | read (`READER=cu`) | `extraction/cu_read.py` | one Azure Content Understanding call reads the whole document, raw result cached forever in `cu_raw/` | IMG API | CU × 1/doc |
| 3 | merge | `extraction/merge.py` | assemble `doc.json` (clean text) + `doc_geometry.json` (bboxes) | DET | none |
| 4 | table_repair | `extraction/table_repair.py` | re-transcribe only tables that fail the grid gate | IMG LLM | vision × ~1/bad table |
| 5 | understand | `extraction/understand.py` | outline, harvest raw facts, categorise (DET), normalise into typed facts | LLM | reasoning × 2 + text × ~1/category |
| 6 | validate | `extraction/validate.py` | the trust layer: re-find every snippet on its page, validators, bbox enrichment | DET | none |
| 7 | repair | `extraction/repair.py` | backfill required fields that came out null, from the fact's own page only | LLM | text × ~1/incomplete fact |
| 8 | load | `kb/load.py` | facts into the Cosmos `kb` container via `pipeline/store/`, pack-driven, undeclared content quarantined, never dropped | DET | none |
| 9 | docmeta | `kb/docmeta.py` | stamp document metadata + document-family group (timeline needs it) | DET | none |
| 10 | timeline | `kb/timeline.py` | effective-date the documents, detect AMENDS/SUPERSEDES/NOVATES from recital text (gated), re-mark per-parameter currency | DET | none |
| 11 | embed | `extraction/embed.py` | embedding fields on evidence/section/block records (exact in-RAM ranking in dev, native DiskANN on Azure via `COSMOS_VECTOR_MODE`) | LLM | embeddings, batched + cached |

The seam is **`canonical/<id>.json`** after stage 7: typed facts with evidence.
Left of it the LLM extracts. Right of it everything is deterministic
knowledge-base build. Only stages 2 and 4 see pixels, and after stage 4
everything is text or records.

---

## 02 · Stage notes (what is not obvious from the table)

- **(2) read, rapidocr path.** Block geometry is the union of its OCR line
  boxes, the LLM never emits coordinates. A wholesale rewrite or a digit swap on
  a confident OCR line is refused and reverts to the OCR text.
- **(2) read, cu path.** Content Understanding returns text, layout and tables
  in one shot, so there is no separate OCR or vision-cleanup call. The raw
  analyze result is cached in `cu_raw/`, so re-runs are free.
- **(4) table_repair.** Fires only past a quality gate (roughly >35% empty
  cells). Repaired rows get per-row geometry sidecars (`<block_id>:rN`), which is
  what makes row-precise table highlights possible.
- **(5) understand.** Four sub-steps: outline (1 call), raw-fact harvest
  (1 call, or per-unit if `fact_source: unit`), categorise (deterministic
  label-to-category via the ontology, with unmapped labels recorded to
  `quarantine/<id>.extraction_drops.json`, never silently dropped), normalise
  (~1 call per non-empty category).
- **(6) validate.** No LLM. Re-finds each cited snippet on its page
  (hallucination check), runs per-category value validators, enriches bboxes,
  cross-checks party roles, derives money values from defined terms, floors
  confidence. A doc that skips validate loses row-precise table rects.
- **(8) load.** Idempotent upserts through `pipeline/store/`, the only layer
  that talks to Cosmos. Pack-driven, no hardcoded labels. Derived edges
  (`kb/derive.py`) join parties, link co-located facts
  (`CONDITIONED_ON`, `TRIGGERED_BY`, ...) and mint `CanonicalParty` /
  `CanonicalSite` hubs. **No LLM-asserted edges**: the build derives
  relationships, the LLM does not.
- **(10, 11) docmeta + timeline.** The same cross-doc passes `rebuild_kb` runs,
  so a single-doc ingest slots the new doc into its contract family and the
  supersedence/currency layer without a full rebuild.

---

## 03 · Storage map

| Subdir | Written by | Read by | Holds |
|---|---|---|---|
| `raw/` | intake | render | input PDFs |
| `pages/` | render | OCR + vision stages | page PNGs |
| `rapidocr/` | rapidocr (`READER=rapidocr`) | read, merge, table_repair, validate | OCR lines: boxes, text, scores |
| `cu_raw/` | cu_read (`READER=cu`) | cu_read | the raw Content Understanding result, cached forever |
| `pages_md/` | the reader (correct_classify or cu_read) | merge, repair | per-page markdown + blocks |
| `doc/` + `doc_geometry/` | merge (mutated by table_repair) | stages 4 to 8 | clean text + block geometry |
| `outline/` `harvest/` `normalise/` | understand | understand chain | intermediate extraction |
| `canonical/` | understand, then validate, then repair | load | final typed facts + overlays |
| `fields/` | the schema-first field extractor | review workflow, build_km | per-field extractions + evidence |
| `quarantine/` | categorise, load, ground | human review | everything refused, never dropped |
| `emb_cache/` | embed | embed | vectors, so rebuilds are free |
| `runs/` | all | debugging | per-stage logs + token usage |

`table_repair/`, `correct_classify/`, `pages_md_rapidocr/`, `structural_units/`,
`unit_extract/` are stage caches.

---

## 04 · The declared contract

Two YAML layers govern the knowledge base. Tests fail the build on drift
(`tests/extraction/test_ontology_compile.py`, `tests/extraction/test_pack.py`):

- [`configs/ontology/`](../configs/ontology/README.md):
  the allowed node labels and edge types, each with provenance
  (`loader` | `derived` | `text_match`).
- [`configs/packs/`](../configs/packs/README.md): the build contract:
  fact shapes, required fields, identity hubs, derivation rules.

---

## 05 · Where the money goes

| Model | Stages | Calls per doc |
|---|---|---|
| vision (gpt-5.4-mini) | read cleanup, table_repair | ~1/page + ~1/failing table (`READER=rapidocr` path) |
| Azure Content Understanding | cu_read | 1/doc (`READER=cu` path, replaces both the OCR and the vision cleanup) |
| reasoning (gpt-5.4) | outline, harvest | 2 |
| text (gpt-5.4-mini) | normalise, repair | ~1/category + ~1/incomplete fact |
| embeddings | embed | batched, cached |
| none | render, merge, categorise, validate, load, ground, docmeta, timeline | 0 |

Everything LLM is content-addressed cached: a re-ingest with no input change
costs $0, and the trust layer (validate) plus the whole knowledge-base build are
free. That is also why `python -m scripts.rebuild_kb` can drop and repopulate
the Cosmos container from `storage/` at no cost. Measured cost: about $1.13 per
document on the RapidOCR reader path. See
[Getting started](getting-started.md#what-this-costs) for the breakdown.
