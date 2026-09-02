# _universal: the base analyzer

The fallback analyzer, used whenever no doctype-specific analyzer matches. It
declares the universal **category** vocabulary that any document could express,
and **no roles** of its own: typed analyzers add roles by extending it.

## What this analyzer declares

```yaml
id: _universal
version: 2.0
# no classify_when: this is the fallback

categories:
  - organization
  - person
  - place
  - equipment
  - money            # specific amount with currency (S$ 300,000)
  - rate             # per-unit rate (S$ 3.20 per RT-hr, 8% per annum)
  - formula          # symbolic calculation (NBV + 25% admin fee, SIBOR + 3%)
  - cost_category    # named cost concept without an amount (Additional Costs)
  - date
  - measurement
  - obligation
  - right
  - condition
  - event
  - defined_term
  - reference
  - schedule

roles: {}            # universal makes no claim about roles

confidence_thresholds:
  verified:  0.80
  tentative: 0.60
```

17 categories. A doctype analyzer does `extends: _universal` to inherit this
list and layer its own roles on top.

## Why this is the fallback

A document with no matching doctype analyzer should still be useful. The 17
universal categories give the extraction a complete vocabulary, so every fact
still gets a category, just `role: null`. That alone supports search,
citation, and entity walks.

## When to edit this

* **Adding a category is rare** and not local to this file. The 17 already
  cover engineering contracts well. A new category must also be added to the
  `categories` enum in `configs/schemas/analyzer.schema.json` and given a
  `fact_types` entry in the doctype pack (`configs/packs/<doctype>.yaml`) plus
  a node label in `configs/ontology/<doctype>.yaml`.
* **Don't add roles here.** Roles are doctype-specific: they belong in a
  sibling analyzer that does `extends: _universal`.

## Confidence thresholds

`verified: 0.80, tentative: 0.60`, slightly looser than typed analyzers,
which have role context to lean on and so set a higher bar.
