# Define a document domain

A domain is the set of document types, fields, relationships, and review rules
for one DIVA instance. The application code is shared; the domain files tell it
what to look for.

The repository ships `commercial_agreement` as a working example. Copy its
shape and change the vocabulary for another kind of record.

## The four required files

| File | Answers this question |
| --- | --- |
| `configs/analyzers/<domain>/analyzer.yaml` | Which document and party categories exist? |
| `configs/ontology/<domain>.yaml` | Which record labels and relationships may be written? |
| `configs/packs/<domain>.yaml` | How do extracted facts map into records and derived links? |
| `configs/views/<domain>_ops.yaml` | Which named fields should a reviewer see? |

The field view is usually the best place to start. Write down the questions a
person needs answered, then describe the fields that answer them.

## 1. Start with fields

A field has a stable key, a human title, a value type, and guidance for finding
its evidence.

```yaml
license_fee:
  title: Licence Fee
  type: value
  multiplicity: 1
  hint: "The fee payable for the licence or subscription."
  source:
    mechanism: value
    label: Payment
```

Keep one idea per field. If users need the amount, currency, and effective date
separately, define fields that can be checked separately rather than asking one
field to contain a paragraph.

Useful value types in the shipped view include text, value, date, boolean, and
lists. Check the existing ops view and its schema definitions before adding a
new shape.

## 2. Describe the document types

The analyzer identifies the categories used by the document reader and the
party roles used by the relationship builder. Start from the shared
`_universal` analyzer and override only what belongs to the domain.

```yaml
extends: _universal
categories:
  - agreement
  - amendment
party_roles:
  - licensor
  - licensee
```

The names used here must match the names accepted by the pack and ontology.
Configuration checks report mismatches before a document is processed.

## 3. Define the graph vocabulary

The ontology is the allowed vocabulary for stored records and relationships. It
should include every label the loader writes and no labels that the domain does
not use.

```yaml
nodes:
  - Document
  - Agreement
  - Party
edges:
  - AMENDS
  - SUPERSEDES
```

The `provenance` value on an edge says how it is created, for example by the
loader, deterministic derivation, or a text match. Model output does not create
an unreviewed graph edge by itself.

## 4. Describe the build pack

The pack connects the field results to the knowledge-store records. Extend
`configs/packs/_base.yaml` and declare the fact types, identity hubs, derived
edges, and aggregation rules that apply to the domain.

```yaml
extends: _base
fact_types:
  - license_fee
  - initial_term
edges:
  derived:
    - AMENDS
```

The pack is a closed contract. If an extracted label is not declared, DIVA
keeps it in the quarantine records for review rather than adding a new record
type silently.

## Document relationships

Amendment chains need a mapping from the domain's field names to six roles:

| Role | Meaning |
| --- | --- |
| Document type | What kind of document this is |
| Sequence | Its position in the family |
| Relationship | The term that describes the link |
| Date | The document date |
| Parent | Which earlier document it changes |
| Effective date | When the change takes effect |

The names are domain-specific. One view may call the date `agreement_date`; a
different view may call it `document_date`. Map the role to the field key in the
`document_relationships` category.

```yaml
document_family:
  field_roles:
    document_type: document_type
    relationship_type: relationship_type
    document_date: document_date
    parent_document: parent_document
    effective_date: effective_date
```

The mapping lets DIVA order documents and resolve the current value of each
field separately. A newer document that does not mention a field does not erase
the earlier value.

Fields that describe the document itself rather than an amendable term can set
`supersedes: false`. This prevents a later amendment from replacing stable
metadata such as a party name.

## Evidence settings

Evidence settings say how a field should be found and what a reviewer should
see. Prefer a mechanism that points to the source text or a known block rather
than a broad instruction such as “find anything related to payment.”

```yaml
source:
  mechanism: value
  label: Payment
  match:
    parameter_any: [license_fee, licence_fee]
```

The field extractor cites page blocks. The reader supplies their geometry, and
the review screen uses those rectangles to highlight the evidence.

Fields whose `mechanism` is `external` are not extracted from the PDF. They are
populated by another system or by an operator, so the document model is not
asked to guess them.

## Sensitivity

Classify fields that should not be visible to every role. The API applies the
classification before results reach the model or the browser.

```yaml
sensitivity:
  default_level: general
  categories:
    commercial_terms: confidential
  field_overrides:
    parties.signatory_name: general
```

Use the domain's policy file for category-level rules and keep the labels in
sync with the pack.

## Optional files

| File | Use |
| --- | --- |
| `configs/prompts/field_extract.<domain>.md` | Domain-specific field extraction guidance |
| `configs/policy/sensitivity.<domain>.yaml` | Restricted categories and role rules |
| `configs/sections.yaml` | Heading patterns for section splitting |
| `configs/prompts/chat_*.{domain}.md` | Domain-specific planning, retrieval, or answer wording |

An optional domain prompt replaces the selected generic prompt. Keep the
instructions and examples together so the change is easy to review.

## Activate and validate

One deployment serves one active domain. Set it in the environment:

```dotenv
VERBATIM_DOMAIN=commercial_agreement
```

Or set `domain` in `configs/pipeline.yaml`. If there is exactly one pack, DIVA
can detect it. If there is more than one and no selection, startup stops rather
than choosing one silently.

Run the checks before processing a collection:

```bash
python -m scripts.setup --check
python -m pytest tests/ -q
python -m scripts.render_ontology
```

Then process one representative document and inspect it in **Review**. Check
the field names, evidence rectangles, blank-value behavior, and document-family
links before using the domain on a larger set.

## Authoring checklist

- Start from questions and fields, not from every phrase in the documents.
- Keep field names stable and make their hints specific about value shape.
- Extend `_universal` and `_base` instead of copying shared configuration.
- Quote bare `Yes` and `No` in YAML.
- Decide whether a blank means `Not Stated`, unchanged, or an external value.
- Re-extract after changing the schema; field caches are presence-based.
- Add a small representative document and expected answers to the tests.
- Update this guide or the relevant configuration README with the change.

## Related references

| Topic | Guide |
| --- | --- |
| Configuration tree | [`configs/README.md`](../configs/README.md) |
| Pack format | [`configs/packs/README.md`](../configs/packs/README.md) |
| Analyzer format | [`configs/analyzers/README.md`](../configs/analyzers/README.md) |
| Pipeline artifacts | [Pipeline overview](PIPELINE_OVERVIEW.md) |
| Stored records | [Data model](data_model.md) |
