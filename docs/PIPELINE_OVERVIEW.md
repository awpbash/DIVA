# Pipeline overview

The ingestion pipeline turns one PDF into page images, text, geometry, field
results, and searchable records. The main command is
[`pipeline/ingest.py`](../pipeline/ingest.py):

```bash
python -m pipeline.ingest path/to/document.pdf
python -m pipeline.ingest --all
```

Stages cache their outputs under `storage/`. Re-running an unchanged document
normally reuses those files. Use `--force` when a source, reader, or schema
change requires a fresh result.

## The reader step

`READER` selects how pages become text and geometry:

| Setting | Implementation | When to use it |
| --- | --- | --- |
| `rapidocr` (default) | Local RapidOCR, followed by the vision correction/classification pass | Local development and deployments that want OCR on the application host |
| `cu` | Azure Content Understanding in `pipeline/extraction/cu_read.py` | A cloud reader that returns layout and table information in one call |

The current Docker image does not use the `paddleocr` package. It installs:

```text
rapidocr>=3.0
onnxruntime>=1.17
```

RapidOCR runs PP-OCR-derived ONNX models. In the local path it returns detected
text lines and their boxes. DIVA keeps those boxes and derives larger block
rectangles from them. The correction model can change text or group lines, but
it does not emit citation coordinates.

If the project needs the PaddleOCR Python package itself, that would be an
implementation change to the reader and Dockerfile. The documentation here
describes the image that is currently in the repository.

## Stages in order

| Step | Module | Result |
| --- | --- | --- |
| Render | `pipeline/extraction/render.py` | One PNG per PDF page in `storage/pages/` |
| Read | `rapidocr_ocr.py` or `cu_read.py` | Cached page text, blocks, and reader geometry |
| Correct and classify | `correct_classify.py` for the RapidOCR path | Cleaned text and block kinds, still tied to reader lines |
| Merge | `merge.py` | `doc.json` plus `doc_geometry.json` |
| Repair tables | `table_repair.py` | Optional grid and row geometry for poor table reads |
| Extract fields | `pipeline/kb/field_llm.py` | Values for the active domain view and their evidence |
| Validate | extraction and review helpers | Snippet matches, value checks, and evidence status |
| Load | `pipeline/kb/load.py` | Records and edges in the Cosmos knowledge container |
| Embed | `pipeline/extraction/embed.py` | Cached vectors for semantic search |

The exact combination of later stages depends on the extraction path and the
command being run. The reader, merge, geometry, field, and load boundaries are
the stable interfaces that the application uses.

## The geometry path

```mermaid
flowchart LR
    PAGE[Rendered page] --> OCR[Reader detects lines]
    OCR --> BOXES[Line text + boxes]
    BOXES --> BLOCKS[Blocks and tables]
    BLOCKS --> EVIDENCE[Field evidence]
    EVIDENCE --> HIGHLIGHT[Page highlight]
```

Geometry uses page-relative coordinates after the reader adapter normalises the
page. The evidence record stores the page number and rectangles, so the web
client does not need to rediscover a snippet by searching the PDF.

## Storage map

| Directory or file | Contains |
| --- | --- |
| `storage/raw/` | Original PDFs and intake metadata |
| `storage/pages/` | Rendered page images |
| `storage/rapidocr/` | Raw RapidOCR lines, boxes, and confidence scores |
| `storage/pages_md_rapidocr/` | Intermediate RapidOCR page representation before the correction pass writes `pages_md/` |
| `storage/cu_raw/` | Raw Azure Content Understanding responses |
| `storage/pages_md/` | Reader-neutral page text and blocks |
| `storage/doc/` | Clean merged document text |
| `storage/doc_geometry/` | Block and row rectangles keyed by block id |
| `storage/fields/` | Schema-first field values and evidence |
| `storage/canonical/` | Canonical extraction records used by the load step |
| `storage/emb_cache/` | Cached embeddings |
| `storage/runs/` | Stage logs and token usage |
| `storage/quarantine/` | Content or relationships held out for review |

The `doc.json` and `doc_geometry.json` files are deliberately separate. Models
and text queries use the compact text file, the viewer uses the geometry sidecar
when it draws a citation.

## Rebuild and inspect

```bash
# Read one document from a PDF path.
python -m pipeline.ingest path/to/document.pdf

# Re-run the field extractor for every document after a schema change.
python -m pipeline.kb.field_llm --all --force

# Rebuild the searchable projection without model calls.
python -m scripts.rebuild_kb

# Check the environment and the active domain.
python -m scripts.setup --check
```

For the field schema and document-family settings, see [Domains](domains.md).
For the storage records produced by these stages, see [Data model](data_model.md).
