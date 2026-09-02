## What this changes

<!-- One or two sentences. What is different after this merges? -->

## Why

<!-- The problem. Link the issue if there is one. -->

## How it was verified

<!-- Delete what does not apply. If you skipped one, say so and why. -->

- [ ] `python -m pytest tests/`
- [ ] `python -m ruff check .`
- [ ] `cd web && npm run build`
- [ ] Checked against a real document (required for anything touching rendering,
      OCR geometry, or evidence anchoring: a passing test does not prove a
      highlight lands on the right clause)

## Checks

- [ ] No domain name written as a string literal in `pipeline/`, `api/`, or `scripts/`
- [ ] No per-document special cases
- [ ] New behaviour that could return a value not present in the source document
      is either impossible or explicitly gated
