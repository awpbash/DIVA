# Concepts

DIVA is built around a simple question: can someone check an answer against the
document that supports it? The choices below explain how the application keeps
that check possible while working across a collection of related documents.

## A field schema gives the collection a shared vocabulary

Each document domain defines the fields that matter. Extraction fills those
fields instead of inventing a different label whenever a document uses a new
phrase.

```yaml
license_fee:
  title: Licence Fee
  type: value
  multiplicity: 1
  hint: "The fee payable for the licence or subscription."
```

The schema also describes the value type, whether more than one value is
allowed, and how evidence should be found. A field can be `Not Stated`, and that is
different from a value that was not processed or a value that a reviewer has
not yet checked.

Content that does not fit the configured schema is kept for review or placed in
the quarantine records. It does not silently become a new field in the live
application. See [Domains](domains.md) for the authoring workflow.

## Evidence travels with the value

An extracted field is useful only when its source can be inspected. DIVA keeps
the value, a verbatim snippet, the page number, and page-relative geometry
together. The PDF viewer uses that geometry to highlight the supporting text.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagrams/evidence-chain-dark.svg">
  <img alt="A contract page with two highlighted clauses connected to their extracted fields, while a field marked Not Stated has no source connection." src="diagrams/evidence-chain-light.svg">
</picture>

This is also why the reader records OCR geometry before later extraction steps.
The model may clean up text or group lines into blocks, but it does not choose
the coordinates used for a citation.

## Relationships are data, not just context

The order of documents matters when one document changes another. A document
family records relationships such as `AMENDS`, `SUPERSEDES`, or `NOVATES`, and
the domain maps those relationships to its own fields.

For a field in an amendment family, the current value is the newest value that
establishes that field. A newer document that is silent about the field does not
erase the earlier value. Earlier values stay available as history.

| Document | Licence fee | Initial term |
| --- | --- | --- |
| Master agreement | SGD 48,000 | 3 years |
| First Amendment | SGD 61,500 | Silent |
| Second Amendment | Silent | 5 years |
| Current result | SGD 61,500 | 5 years |

The walk is per field, not per document. This is the behavior demonstrated by
the sample corpus in [`examples/README.md`](../examples/README.md).

## Review is part of the workflow

Processing and review are separate so a collection can become searchable while
people work through the fields that need attention.

| Stage | What it means |
| --- | --- |
| Extracted | A model found a value and its evidence |
| Needs review | The value or its evidence needs a human decision |
| Verified | The configured reviewers approved the value |
| Corrected | A reviewer changed the value or attached better evidence |

Reviewers can approve, reject, or propose a correction. Corrections follow the
configured approval count and remain in the record history. They are not hidden
edits to the source document.

## Use the right retrieval path for the question

Natural-language questions and exact field queries have different shapes.

| Question | Retrieval path |
| --- | --- |
| “Which clause describes the renewal right?” | Search relevant text and return the passage |
| “What is the current licence fee?” | Look up the field and walk its document family |
| “What do these fees total?” | Fetch all matching values and calculate the total in Python |
| “Is there a service-credit regime?” | Search the collection and return `Not Stated` when the domain does not provide evidence |

The model chooses tools and writes the response, while filtering, current-value
resolution, and arithmetic happen in application code. [Retrieval](retrieval.md)
describes the request path in detail.

## Configuration keeps domain rules separate from the engine

The reusable engine lives in Python. A domain supplies analyzers, an ontology,
a pack, and an operational field view. That separation lets a contributor add a
new document type without copying the ingestion or retrieval code.

The same boundary also makes changes easier to review: a change to a field
definition stays in configuration, while a change to evidence anchoring or
query execution stays in the relevant module.

## Further reading

| Topic | Guide |
| --- | --- |
| Runtime components | [Architecture](architecture.md) |
| Ingestion stages and caches | [Pipeline overview](PIPELINE_OVERVIEW.md) |
| Stored records and edges | [Data model](data_model.md) |
| Domain configuration | [Domains](domains.md) |
