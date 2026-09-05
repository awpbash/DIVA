# Documentation images

Product screenshots and branding assets used by the README and the documentation
pages.

The product screenshots here were captured against the synthetic sample corpus
in [`examples/corpus/`](../../examples/corpus): fictional companies, invented
numbers, ordinary boilerplate. That is the rule, and it is the reason they are
safe to publish. A screenshot of this product shows whatever corpus is loaded,
so one taken against a real deployment carries that customer's document titles
and counterparties into every copy of the repository.

| File | Shows |
| --- | --- |
| `logo.png` | The mark-only DIVA icon used at the top of the repository README and as a small repository icon |
| `diva-logo.png` | The light-background DIVA lockup with the friendly robot, document, and full name |
| `diva-logo-dark.png` | The dark-background DIVA lockup with white line art and the full name |
| `chat-start.png` | The cold-start screen, with the kinds of question the two retrieval modes handle |
| `chat-citation.png` | An answer, its citation clicked, and the source clause highlighted on the page |
| `review-field.png` | Verifying a field against the clause it came from |
| `knowledge-supersedence.png` | Current values with their superseded history, and the document that set each one |
| `admin-documents.png` | The admin dashboard after the sample corpus is loaded |
| `real-world-citation.png` | The one deliberate exception, see below |

**One deliberate exception.** `real-world-citation.png` is captured against
[`examples/real_world/`](../../examples/real_world) instead. That corpus is
not a customer deployment, it is public U.S. SEC filings redistributed under
CC BY 4.0, the same basis the [CUAD dataset](../../examples/real_world/README.md)
itself redistributes them on, so showing it carries no one's private
information anywhere. Every other file in this folder keeps to the rule
above.

## Recapturing them

Load the sample corpus by following [Getting
started](../getting-started.md), then capture at a viewport of 1600 by 1000 so
the set stays consistent.

Two things to check before committing a replacement. It must show the sample
corpus and nothing else. And it must not show a value you know to be wrong:
extraction misreads two fields in this corpus on purpose (see
[`examples/README.md`](../../examples/README.md)), and a screenshot is a claim,
so keep those out of frame unless the point of the image is the correction.

## `badges/`

Small logo badges used in [Tech stack](../tech_stack.md), one file per
technology. Each is a static, already-rendered SVG rather than a live link to
a badge service, because two of these logos (Azure, OpenAI) were pulled from
the icon library most badge services draw from over trademark disputes, a
live badge for either would eventually render with no icon at all. Baking the
result in once means the badge either keeps working or is obviously missing,
never silently blank.

Regenerate one with shields.io, for example:

```
https://img.shields.io/badge/Python-3776AB.svg?logo=python&logoColor=white
```

Azure and OpenAI need their real mark supplied by hand as a base64 data URI in
the `logo=` parameter instead of a `logo=` slug, since neither has a working
slug in the icon library shields.io reads from. Source both from
[Iconify's `logos` set](https://icon-sets.iconify.design/logos/), a separately
maintained collection unaffected by that removal, not from simple-icons.
