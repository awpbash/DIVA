# `pipeline/extraction/`

These modules turn rendered pages into text, geometry, and field results. The
shared output is the page representation consumed by merge, review, and the
knowledge build.

For a user-level walkthrough, see [Pipeline overview](../../docs/PIPELINE_OVERVIEW.md).

## Reader choices

`READER` is loaded by `pipeline.config.Config`:

| Value | Modules | Geometry |
| --- | --- | --- |
| `rapidocr` | `render.py` → `rapidocr_ocr.py` → `correct_classify.py` | OCR line boxes, normalised by the adapter |
| `cu` | `render.py` → `cu_read.py` | Polygons returned by Azure Content Understanding |

The Docker image includes `rapidocr` and `onnxruntime`, not the `paddleocr`
package. RapidOCR's models are derived from PP-OCR. The answer model receives
the OCR lines and page image, but it does not emit the rectangles used for
citations.

## Modules

| File | Purpose |
| --- | --- |
| `loader.py` | Compose and validate the domain configuration |
| `schemas.py` | Pydantic models for structured model responses |
| `pack.py` | Compile and validate the domain pack |
| `render.py` | Convert PDFs into page images |
| `rapidocr_ocr.py` | Run local OCR and cache line text, boxes, and scores |
| `correct_classify.py` | Correct OCR text and group lines into page blocks |
| `cu_read.py` | Adapt Azure layout and table responses to the shared page shape |
| `merge.py` | Write `doc.json` and `doc_geometry.json` |
| `table_repair.py` | Repair poor table grids and add row rectangles |
| `understand.py` | Run the legacy outline, harvest, categorise, and normalise passes |
| `canonical_lite.py` | Write the structure-only canonical output used by the field view |
| `validate.py` | Re-find snippets, validate values, and enrich evidence |
| `embed.py` | Write cached semantic-search vectors |
| `textnorm.py` | Compare snippets across plain text and reader markdown |
| `viz_ocr.py` | Draw OCR and block geometry over a rendered page for debugging |

## Artifacts

| Artifact | Written by | Read by |
| --- | --- | --- |
| `storage/pages/` | `render.py` | Reader modules |
| `storage/rapidocr/` | `rapidocr_ocr.py` | Correction, merge, and table repair |
| `storage/pages_md_rapidocr/` | `rapidocr_ocr.py` | Correction pass input |
| `storage/cu_raw/` | `cu_read.py` | CU adapter and diagnostics |
| `storage/pages_md/` | Selected reader | Merge and field extraction |
| `storage/doc/` | `merge.py` | Extraction and retrieval preparation |
| `storage/doc_geometry/` | `merge.py` | Evidence and the PDF viewer |
| `storage/canonical/` | Canonical/validation stages | Knowledge loading |

## Useful commands

```bash
python -m pipeline.extraction.lint
python -m pipeline.extraction.show commercial_agreement
python -m pipeline.extraction.rapidocr_ocr <doc_id> --check
python -m pipeline.ingest <doc_id>
```

Use `pipeline.kb.field_llm` for the active field schema:

```bash
python -m pipeline.kb.field_llm --doc <doc_id>
python -m pipeline.kb.field_llm --all --force
```

## Testing a change

Pure parsing and geometry helpers should be tested without a model or database.
The extraction tests use injected model callables and disk fixtures. Start with
[`tests/extraction/README.md`](../../tests/extraction/README.md), then run:

```bash
python -m pytest tests/extraction/ -q
```
