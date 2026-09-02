# pipeline/

The ingestion runtime: a scanned contract PDF becomes typed facts and then a
knowledge base in Azure Cosmos DB NoSQL. All domain knowledge (what to extract,
how to validate, the graph ontology) lives in the `configs/` tree at the repo
root. This package is the engine that runs it.

Two halves, joined at the **`canonical.json` seam**:

- **`extraction/`**: per-doc, LLM-driven. `PDF → storage/canonical/<doc>.json`
  (typed facts + their evidence spans). The LLM extracts facts *only*.
- **`kb/`**: deterministic. `canonical.json` + the compiled Doctype Pack → the
  Cosmos knowledge base. All structural, cross-document, and derived edges are
  computed here.

## Layout

```
pipeline/
├── ingest.py     one-command driver, runs the whole chain for a doc (idempotent)
├── config.py     runtime env: API keys, model names, storage_root, Cosmos settings, reader switch (Config.load reads .env)
├── storage.py    on-disk path layout, the single source of truth for storage/… locations (atomic writes)
├── ontology.py   loads the declared graph ontology (node labels + edge types)
├── store/        the ONLY layer that talks to Cosmos: client.py / aio.py (sync + async stores), query.py (SQL builder, emulator pk injection), model.py (item shapes)
├── extraction/   per-doc reading + LLM structuring (see pipeline/extraction/README.md)
└── kb/           deterministic knowledge-base build (see below)
```

## `kb/`: the pack-driven graph layer

Consumes `canonical.json` + the compiled Doctype Pack and builds the knowledge
base. Everything that used to be a hardcoded Python dict is now read from the
pack, so a new doctype is a new pack with **zero loader changes**.

| Module | Role |
|---|---|
| `load.py` | Pack-driven Cosmos loader: container ensure, nuke, per-doc load. Writes `Document / Agreement / Section / Block / EvidenceSpan` records plus dual-label fact items (`labels: ["Fact", <Type>]`). Derives fact-to-fact edges and mints identity hubs internally. |
| `derive.py` | The pack-driven derivation engine: computes structural / co-location edges (`CONDITIONED_ON`, `TRIGGERED_BY`, `COMPUTES`, …) as deterministic Python joins over the doc's own records, never from the LLM. |
| `writers.py` | Doctype-invariant structural writers + pure helpers (normalisation keys, evidence-rect derivation) shared across `kb.*`. |
| `timeline.py` | Recital-text detection of `AMENDS` / `SUPERSEDES` / `NOVATES` between agreements + effective-date agreements (gated, no re-extraction). |
| `docmeta.py` | Document display metadata + entity→document resolution support. |
| `datetype.py` | Load-time re-typing of unreliably-labelled dates (pack `derivations.date_typing`). |
| `registry.py` · `intake.py` | Named document families and declared document metadata (SQLite rows in `storage/app.db`), plus the declared-intake sidecar: what an uploader declares about a document beats what the PDF happens to say. |
| `assets.py` | `DocAsset` pointers to companion files so chat can surface them without re-ingestion. |
| `concepts.py` · `paramfacets.py` · `paramconcepts.py` · `paramcanon.py` · `vocab.py` | The parameter/concept layer: canonical parameter names, core+topic facets, and broad concept families, the comparable axes that retrieval filters and supersedence groups on. |
| `opsview_spec.py` | Compiles the active domain's `configs/views/<domain>_ops.yaml` (the field schema, the scored extraction target), drift-gated by the contract tests. |
| `field_llm.py` | The schema-first ops-view field extractor: fills the ops-view fields from the document text, evidence-anchored, → `storage/fields/`. |
| `ontology_store.py` | SQL overlay (rows in `storage/app.db`) over the base view yaml: admin field edits with attribution, while the base schema stays in git. |
| `km.py` / `km_query.py` | The aligned KM layer: `OpsField` records with trust tier + evidence, canonical-party hubs, the recital-declared document DAG, and per-field supersedence, plus its query helpers for the API/chat tools. Built by `python -m scripts.build_km` (also folded into `rebuild_kb`). |

## `ingest`: the one command

```sh
python -m pipeline.ingest path/to/contract.pdf   # intake a PDF + run the full chain
python -m pipeline.ingest <doc_id>                # re-run a doc already in storage/raw/
python -m pipeline.ingest --all                   # every pdf in storage/raw/
python -m pipeline.ingest --force                 # bypass caches (full re-extraction, costs LLM calls)
```

Every stage is idempotent: content-addressed caches on the extraction side,
upsert-on-key writes on the store side. Re-running an already-ingested doc is
cheap no-ops all the way down. To rebuild the whole knowledge base from local
caches without re-extracting: `python -m scripts.rebuild_kb` (free, embeddings
restored from `storage/emb_cache`).

## Two kinds of config

| Path | What it is |
|---|---|
| `pipeline/config.py` | Env-driven runtime (`Config.load()` reads `.env`): API keys, model names, `storage_root`, Cosmos URI / key / container / vector mode, and the reader switch (`READER=rapidocr` or `cu`). Imported as `from pipeline.config import Config`. |
| `configs/` (repo root) | The declarative tree: analyzers (categories + roles), packs (the ontology), prompts, validators, meta-schemas. **Edit YAML/Markdown here, not Python, to change extraction or graph behaviour.** |

## Data flow

```
storage/raw/<doc>.pdf
   │  READER=rapidocr (default): render → rapidocr → correct_classify
   │  READER=cu:                 cu_read (Azure AI Content Understanding, same pages_md shape)
   │  then: merge → table_repair
   ▼
storage/doc/<doc>.json (+ doc_geometry sidecar)
   │  understand (outline → harvest|unit → categorise → normalise) → validate → repair
   ▼
storage/canonical/<doc>.json          ◄── the seam: typed facts + evidence spans
   │  kb.load (+ derive edges + mint hubs) → kb.ground → embed
   ▼
Azure Cosmos DB NoSQL (one container holds records, edges and vectors)
```

## Design rules

1. **The LLM extracts facts only.** It never emits graph edge names. All edges are
   derived deterministically at load (`kb/derive.py`) or asserted with evidence and
   gated (`kb/timeline.py`).
2. **Pure first, IO at the edge.** Stages expose pure `build_*` / `parse_*` helpers.
   Async LLM stages accept an injectable `llm_call=` so tests run fully offline.
3. **Content-addressable caching.** Each stage caches on the SHA of its input, so
   editing one prompt invalidates only that pass and everything downstream.
4. **The ontology is declared, not coded.** Node labels and edge types live in
   `configs/ontology/` + `configs/packs/`. A drift gate fails the build on changes
   that aren't declared.
