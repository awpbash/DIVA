# Screenshots

Product screenshots used by the README and the documentation pages.

Everything here was captured against the synthetic sample corpus in
[`examples/corpus/`](../../examples/corpus): fictional companies, invented
numbers, ordinary boilerplate. That is the rule, and it is the only reason
these are safe to publish. A screenshot of this product shows whatever corpus
is loaded, so one taken against a real deployment carries that customer's
document titles and counterparties into every copy of the repository.

| File | Shows |
| --- | --- |
| `chat-start.png` | The cold-start screen, with the kinds of question the two retrieval modes handle |
| `chat-citation.png` | An answer, its citation clicked, and the source clause highlighted on the page |
| `review-field.png` | Verifying a field against the clause it came from |
| `knowledge-supersedence.png` | Current values with their superseded history, and the document that set each one |
| `admin-documents.png` | The admin dashboard after the sample corpus is loaded |

## Recapturing them

Load the sample corpus by following [Getting
started](../getting-started.md), then capture at a viewport of 1600 by 1000 so
the set stays consistent.

Two things to check before committing a replacement. It must show the sample
corpus and nothing else. And it must not show a value you know to be wrong:
extraction misreads two fields in this corpus on purpose (see
[`examples/README.md`](../../examples/README.md)), and a screenshot is a claim,
so keep those out of frame unless the point of the image is the correction.
