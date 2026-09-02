# tests/

Fast, deterministic unit tests for the extraction pipeline, the knowledge-base +
KM layers, the retrieval helpers, and the API auth/RBAC gates. **No LLM, no
network, no DB**: every async LLM call takes an injected fake, and store logic is
tested through its pure helpers and in-memory fixtures. The whole suite is
about **900 tests in seconds**.

## Run

```sh
.venv/Scripts/python -m pytest tests/ -q              # everything
.venv/Scripts/python -m pytest tests/extraction/ -q   # one subtree
.venv/Scripts/python -m pytest -k validators          # by keyword
.venv/Scripts/python -m pytest -x tests/              # stop at first failure
```

## Layout

```
tests/
├── conftest.py     shared fixtures
├── extraction/     pipeline/extraction/ + configs/  (stages, parsing, gating, extraction modes)
├── kb/             pipeline/kb/  (load, concepts/facets, ops view, timeline, KM layer, ontology store)
├── rag/            api/rag/  (citations, hybrid fusion, label filters, policy roles)
├── api/            api/  (auth dependencies, review votes, admin intake, the RBAC access matrix)
└── eval/           eval/ helpers
```

## What's covered

Pure logic and orchestration: config composition, deterministic categorisation and
edge derivation, value validators, entity-resolution keys, the quarantine/proposal
gate, and citation parsing. The LLM-driven passes (correct +
classify, outline, harvest, normalise, synth) are exercised through injected fakes.
We test the parsing, dispatch, and gating around the model, not the model's output.

Answer **quality** is evaluated against a running pipeline and a gold set you
create for your own domain with `eval/extraction/score.py`, not unit-tested.
That is a property of the live system, not of pure code.
