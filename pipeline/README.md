# `pipeline/`

This package processes PDFs and builds the searchable knowledge projection. The
reader-facing explanation is [Pipeline overview](../docs/PIPELINE_OVERVIEW.md);
this page is for contributors changing the implementation.

## Layout

```text
pipeline/
├── ingest.py       one-command document driver
├── config.py       environment and reader settings
├── storage.py      paths and atomic file writes
├── ontology.py     active domain selection
├── extraction/     PDF reading, geometry, field extraction, and validation
├── kb/             records, relationships, current values, and field views
└── store/          the only Cosmos DB access layer
```

## Main boundaries

| Boundary | Input | Output |
| --- | --- | --- |
| Reader | Rendered page image | Page text, blocks, and geometry |
| Field extraction | Merged document text and active view | Named values and evidence |
| Knowledge build | Extraction files and domain pack | Cosmos records, edges, and vectors |
| Retrieval | Knowledge records and caller role | Scoped evidence for the API |

The reader and field stages can call a model. Categorisation, geometry lookup,
record construction, relationship derivation, and knowledge rebuilding are
handled by application code.

## Run the pipeline

```bash
python -m pipeline.ingest path/to/document.pdf
python -m pipeline.ingest <doc_id>
python -m pipeline.ingest --all
python -m pipeline.ingest <doc_id> --force
```

Documents are stored under `storage/raw/` using a content-derived id. Stage
artifacts are cached under `storage/`; use `--force` when a source, reader, or
schema change requires new output. Rebuild the database projection without
model calls with:

```bash
python -m scripts.rebuild_kb
```

## Configuration boundary

Runtime settings live in `pipeline/config.py` and come from `.env`.
Domain behavior lives in the root `configs/` tree. To add a document type,
change the analyzer, pack, ontology, view, or prompt before adding a special
case to the pipeline.

## Knowledge-base modules

| Module | Responsibility |
| --- | --- |
| `kb/load.py` | Write document structure and field records to Cosmos |
| `kb/derive.py` | Derive declared relationships from stored facts |
| `kb/timeline.py` | Build document-family links and effective dates |
| `kb/field_llm.py` | Fill the active field view with evidence |
| `kb/km.py`, `kb/km_query.py` | Build and query aligned current-value records |
| `store/` | Cosmos client, queries, and item models |

All relationship types and domain-specific labels must come from configuration.
Tests should cover pure helpers without requiring a live database.

## Related contributor notes

- [Extraction modules](extraction/README.md)
- [Configuration](../configs/README.md)
- [Data model](../docs/data_model.md)
- [Contributing](../CONTRIBUTING.md)
