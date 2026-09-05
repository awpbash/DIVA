# `tests/extraction/`

These tests cover `pipeline/extraction/`, the domain configuration loaders, and
the pure evidence helpers used by the knowledge build. They run offline and
complete quickly.

## Areas covered

| Area | Examples |
| --- | --- |
| Configuration | Analyzer composition, pack compilation, ontology mappings, prompts, and validators |
| Readers | RapidOCR output and Azure Content Understanding response adaptation |
| Geometry | Block and row rectangles, page coordinates, and evidence lookup |
| Pipeline stages | Rendering helpers, merge, table repair, understanding, validation, and repair |
| Field extraction | Schema-first extraction, evidence markers, and cache behavior |
| Proposal channel | Relation proposal validation and review promotion |
| Storage helpers | Atomic writes and content-addressed paths |

## Run them

```bash
python -m pytest tests/extraction/ -q
```

## Test model-driven code

LLM stages accept an injected async callable. Tests provide a small fake response
and check parsing, dispatch, gating, and output shape. They do not try to prove
that a particular model will extract a real document correctly.

## Add a regression

Use a minimal fixture that isolates the behavior. If the case depends on a
document layout, include a small synthetic page or a public fixture and assert
both the text result and its geometry. Update
[Pipeline overview](../../docs/PIPELINE_OVERVIEW.md) when a new artifact or
stage becomes part of the public behavior.
