# configs/analyzers/

One folder per doctype. Each folder declares **what categories of facts to
extract**, **what roles to assign** within those categories, and **when this
analyzer applies**.

```
analyzers/
|-- _universal/              ALWAYS available. The category vocabulary, no roles.
|   `-- analyzer.yaml
`-- <domain>/                extends _universal, adds the typed role taxonomy.
    `-- analyzer.yaml
```

Analyzers carry no per-field extraction directives: they are category +
role taxonomies. The graph shape (node properties, edges) is the pack's job
(`configs/packs/`), not the analyzer's.

## How it composes

`pipeline/extraction/loader.py` treats each subfolder as one analyzer. For
each:

1. `analyzer.yaml` is validated against `configs/schemas/analyzer.schema.json`.
2. **Folder name MUST equal `analyzer.yaml: id`**. The loader rejects a
   mismatch.
3. If `extends: _universal`, the universal `categories` are inherited (the
   child may re-declare a narrower list). The child's `roles` block is then
   merged on top per category. A child role list **replaces** the parent's for
   that category rather than appending.
4. Every category named in `roles` must appear in `categories`, or the load
   fails.
5. The result is a frozen `ComposedAnalyzer` (categories + `roles_by_category`),
   cached by a stable SHA over all inputs plus the relevant pipeline defaults.

## Categories vs roles

* **Category**: what KIND of fact this is. One of 17 categories declared by
  `_universal`:

  ```
  organization | person | place | equipment |
  money | rate | formula | cost_category |
  date | measurement |
  obligation | right | condition | event |
  defined_term | reference | schedule
  ```

  A doctype enables a subset, and may add its own. The category
  set is also the `enum` in `analyzer.schema.json`, so a typo'd category fails
  validation.

* **Role**: within a category, which specific slot does this fact fill? For
  example `money` → `deposit | termination_fee | lump_sum_transfer`, or
  `obligation` → `supplier_obligation | customer_obligation | mutual_obligation`.
  Roles are doctype-specific.

The normalise pass assigns one role per fact when it matches a defined role for
that category. Otherwise the fact keeps `role: null`. A category with no roles
defined for the active doctype is still extracted: its facts simply carry no
typed slot.

## Confidence thresholds

An analyzer may override `pipeline.yaml`'s `load.confidence` defaults with its
own `confidence_thresholds` block. `_universal` uses `verified: 0.80,
tentative: 0.60`. A domain with costlier mistakes should tighten it, for example to
`0.85 / 0.65`.

## Adding a new doctype

```sh
mkdir configs/analyzers/<your_doctype>/
```

1. Write `analyzer.yaml`: `id` (= folder name), `version`, `extends: _universal`,
   `classify_when`, `categories`, `roles`.
2. `python -m pipeline.extraction.lint`: validates every analyzer. Fix until
   green.
3. `python -m pipeline.extraction.show <your_doctype>`: print the composed
   analyzer (add `--category <c>` to see one category's roles).

No prompts to author, no Python changes.

## Versioning

Bump `version:` in `analyzer.yaml` whenever categories or roles change. The
loader's `cache_key` includes the version plus the composed categories and
roles, so the normalise pass and downstream load invalidate naturally.
