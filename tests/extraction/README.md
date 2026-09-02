# tests/extraction/

Unit tests for `pipeline/extraction/`, `configs/`, the atomic-write helpers in
`pipeline/storage.py`, and the pure helpers in `pipeline/kb/writers.py`. Pure
functions and injected-LLM orchestration only: no LLM, no DB, no network. Runs
in well under 2 seconds.

## Files

### Config + composition
| File | Module under test |
|---|---|
| `test_loader.py` | `loader.py`: category + role composition, role inheritance |
| `test_pack.py` | `pack.py`: Doctype Pack compilation + validation |
| `test_ontology_compile.py` | `ontology_compile.py`: label↔category maps, ontology drift gate |
| `test_prompt_helpers.py` | `prompt_helpers.py`: pure Jinja-env helpers |
| `test_validators.py` | `configs/validators/*.py` (money / date / company_name / reference / snippet) |

### Pipeline stages
| File | Module under test |
|---|---|
| `test_rapidocr_ocr.py` | `rapidocr_ocr.py`: OCR adaptation to the block model |
| `test_correct_classify.py` | `correct_classify.py`: parsing + block classification with an injected LLM |
| `test_cu_read.py` | `cu_read.py`: Content Understanding response adaptation (markdown, tables, polygon geometry → the shared `pages_md` shape), from disk fixtures |
| `test_merge.py` | `merge.py`: page ordering + geometry sidecar split + caching |
| `test_understand.py` | `understand.py`: pure context-builders + parsers, pass orchestration |
| `test_structural_units.py` | `structural_units.py`: deterministic clause/definition/table segmentation |
| `test_unit_extract.py` | `unit_extract.py`: per-unit extraction with an injected LLM |
| `test_categorise.py` | `categorise.py`: raw_label dispatch, exact-text dedup, regex cross-refs |
| `test_normalise.py` | `normalise.py`: per-category bundling + role assignment |
| `test_text_repair.py` | `text_repair.py`: numeric-confusable value repair |
| `test_textnorm.py` | `textnorm.py`: markdown/HTML neutralisation for snippet matching (both-sides normalisation, unicode folding) |
| `test_validate.py` | `validate.py`: snippet re-find, value validators, bbox enrichment |
| `test_token_meter.py` | `token_meter.py`: per-stage token accounting |
| `test_storage_atomic.py` | `pipeline/storage.py`: atomic JSON/text writes (all-or-nothing, no tmp leftovers) |
| `test_extraction_mode.py` | `loader.py` / `canonical_lite.py`: `extraction_mode` resolution (env > CLI > yaml) + the fact-less schema-first canonical |

### Proposal channel
| File | Module under test |
|---|---|
| `test_relation_propose.py` | `relation_propose.py`: proposal generation + domain/range validation |
| `test_proposal_review.py` | `proposal_review.py`: review / promote / reject flow |

### KB pure helpers (in `pipeline/kb/writers.py`)
| File | What it covers |
|---|---|
| `test_fact_mentions.py` | `_mention_rects`: span `block_ids` → per-page highlight rects + union envelope (the bbox geometry the citation path depends on) |
| `test_graph_links.py` | text-match link derivations: cross-reference / schedule / term-usage pairing (pure matchers, the store writes are exercised by `rebuild_kb`) |
| `test_quarantine.py` | suffix-invariant identity-key helpers: the canonical keys behind cross-doc entity resolution |

## Conventions

1. **One concept per test.** The test name is a sentence describing the property
   (e.g. `test_find_block_bbox_unions_across_blocks_when_no_single_block_matches`).
2. **Parametrize over loops.** Parametrized cases surface individually in pytest
   output, which makes triage easier on a regression.
3. **Pure functions get pure tests.** No `tmp_path` unless the function does IO.
4. **LLM calls are injected, never patched.** Stages accept an `llm_call=` kwarg.
   Tests pass a tiny async stub returning canned dicts keyed by schema name. No
   `unittest.mock` of the OpenAI client.
5. **Async stages run under `asyncio.run`** inside sync test functions, so no
   pytest-asyncio plugin is needed.
