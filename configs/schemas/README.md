# configs/schemas/

JSON meta-schemas for the configs **themselves**, not for the documents the
pipeline ingests.

| File | Validates |
|---|---|
| `analyzer.schema.json` | `configs/analyzers/<doctype>/analyzer.yaml` |

## Why a meta-schema

So a malformed analyzer is caught at load time, not after the pipeline has
already burned tokens. An editor who writes `confidence_thresholds: { verified: 7 }`
(forgot the decimal) gets:

```
configs/analyzers/<domain>/analyzer.yaml: schema violation:
  - confidence_thresholds.verified: 7 is greater than the maximum of 1
```

`analyzer.schema.json` constrains the analyzer's full shape: the `id` pattern
(must match the folder name), `version`, the `categories` names, the
`roles.<category>` pattern, `classify_when`, and the `confidence_thresholds`
range `[0, 1]`. Analyzers declare no per-field directives, so there is nothing
else to validate.

Category names are an open `^[a-z][a-z0-9_]*$` pattern, not a closed list. They
used to be a fixed enum of the first domain's seventeen categories, which meant
a new domain could not name a category its own corpus actually contains. What
protects the system now is agreement rather than enumeration: a category the
ontology declares but the analyzer does not, or the reverse, fails a contract
test, so an invented name still cannot go unnoticed.

## How the loader uses it

`pipeline/extraction/loader.py: _validate` runs every `analyzer.yaml` through
this meta-schema at load. It is **fail-fast**: a single malformed analyzer
prevents the pipeline from running, and the error names the offending file.

## Editing the meta-schema

Tighten freely (catch more bugs). Loosen with care: any existing analyzer that
violates a newly tightened shape will start failing.

If you add a new analyzer-level key:

1. Add the property under `analyzer.schema.json: properties` (note
   `additionalProperties: false`, unknown keys are rejected).
2. Update `pipeline/extraction/loader.py` to read it.
3. Document it in `configs/analyzers/README.md`.
4. Add a regression test in `tests/extraction/test_loader.py`.
