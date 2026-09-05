# `tests/`

The test suite covers the pipeline, configuration, knowledge build, retrieval,
and API access rules. Tests use fixtures and injected model calls, so the normal
suite does not need a model endpoint, live Cosmos account, or network access.

## Run the tests

```bash
python -m pytest tests/ -q
python -m pytest tests/extraction/ -q
python -m pytest -k citations
python -m pytest -x tests/
```

## Directory map

```text
tests/
├── extraction/   readers, geometry, config, prompts, and field extraction
├── kb/            loading, relationships, current values, and knowledge views
├── rag/           retrieval, citations, policy, and tool behavior
├── api/           sessions, review, admin, and route access
└── eval/          evaluation helpers
```

## What the tests check

- configuration composition and pack validation
- OCR and Content Understanding response adaptation
- page geometry, snippet matching, and evidence anchoring
- field extraction orchestration through fake model calls
- deterministic record and relationship construction
- vector and keyword retrieval helpers
- citation parsing and removal of unknown evidence ids
- server-side roles, sensitivity filters, review votes, and route gates

The tests check the application around model calls, not the quality of a live
model's output. Evaluate extraction quality with a human-verified gold set:

```bash
python -m eval.extraction.score --run NAME
```

## Testing conventions

Keep tests focused on one behavior. Prefer pure helper tests and temporary
directories only for functions that perform file I/O. Pass a small async fake to
LLM-driven stages instead of patching the OpenAI client. Add a regression test
when a document, schema, or real-world corpus exposes a new case.

For the contributor workflow, see [Contributing](../CONTRIBUTING.md).
