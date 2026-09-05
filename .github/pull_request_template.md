## What this changes

<!-- One or two sentences. What is different after this merges? -->

## Why

<!-- The problem. Link the issue if there is one. -->

## How it was verified

<!-- Delete what does not apply. If you skipped one, say so and why. -->

- [ ] `python -m pytest tests/`
- [ ] `python -m ruff check .`
- [ ] `cd web && pnpm run build && pnpm test`
- [ ] Checked against a real document (required for anything touching rendering,
      OCR geometry, or evidence anchoring: a passing test does not prove a
      highlight lands on the right clause)

## Checks

- [ ] Domain-specific behavior lives in configuration rather than a string
      literal or a per-document special case
- [ ] New behavior that could return a value not present in the source document
      is either impossible or explicitly gated
- [ ] Documentation, examples, or generated figures are updated when needed
