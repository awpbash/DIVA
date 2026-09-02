# Examples

A sample contract family, and the questions to ask it.

Everything here is synthetic. Fictional companies, fictional people, invented
numbers, ordinary commercial boilerplate. None of it is legal drafting and
none of it should be used as such.

## The corpus

Three PDFs in [`corpus/`](corpus), for the `commercial_agreement` domain that
ships with the repository.

| File | What it is | Dated |
| --- | --- | --- |
| `01-master-license-agreement-2023.pdf` | The base agreement. Sets every field once | 14 March 2023 |
| `02-first-amendment-2024.pdf` | Changes the licence fee and the liability cap. Silent on everything else | 2 September 2024 |
| `03-second-amendment-2025.pdf` | Extends the term and deletes the termination right. Silent on the fee | 20 January 2025 |

The parties are Northwind Logistics Pte. Ltd. as licensor and Fairhaven
Systems Limited as licensee.

## Why this shape

This corpus is a test, not a demo prop. The three documents are arranged so
that answering correctly requires walking the amendment chain field by field.

**The current licence fee is in the middle document.** The base agreement says
SGD 48,000. The First Amendment changes it to SGD 61,500. The Second
Amendment, which is the newest document, says nothing about the fee at all.

**The current initial term is in the newest document.** The base agreement
says three years. The First Amendment is silent. The Second Amendment changes
it to five years.

So no single reading rule gets both right. "Use the newest document" gets the
fee wrong. "Use the base agreement" gets the term wrong. "Use the newest
document that mentions the field" gets both right, and that is the walk.

The Second Amendment also deletes a clause outright, which is a third case
again: the correct current answer is that there is no termination for
convenience, even though two of the three documents say there is.

## Loading it

Follow [Getting started](../docs/getting-started.md) to the point where the app
is running, then upload the three files in order from the Admin tab. Put them
in one folder and declare each amendment as amending the master agreement:
those declarations are what make the three a chain.

Reading all three cost about 27,000 tokens the last time this page was
checked, plus one schema-filling pass per document. Every stage is cached, so
running it again does nothing and spends nothing.

## What the documents say

The correct current value of every field, read from the documents. Use this to
check what your instance says. The "from" column is the document that sets the
value, which is the interesting part.

| Field | Current value | From |
| --- | --- | --- |
| Licence fee | SGD 61,500 per year | First Amendment |
| Minimum commitment | SGD 120,000 over the initial term | Base, inherited through both amendments |
| Cap on liability | SGD 400,000 | First Amendment, raised from SGD 250,000 |
| Initial term | Five years, expiring 31 March 2028 | Second Amendment |
| Renewal term | Twelve months, automatic | Base |
| Survival period | Five years after termination | Base |
| Termination for convenience | No | Second Amendment deleted the clause |
| Audit right | Yes | Base |
| Renewal right | Yes | Base |
| Governing law | The laws of Singapore | Base |
| Venue | The courts of Singapore | Base |
| Disclosing party | Northwind Logistics Pte. Ltd. | Base |
| Receiving party | Fairhaven Systems Limited | Base |
| Confidentiality obligation on | Receiving Party | Base |
| Payment obligation on | Licensee | Base |
| Internal matter number | Not Stated | Nowhere. It is operational metadata, not contract text |

## Questions to ask

Seven of these should be answered from the documents with a citation. Three
should be answered "not in the documents", and getting those right matters as
much as getting the others right.

| # | Ask | Correct answer | Tests |
| --- | --- | --- | --- |
| 1 | What is the current annual licence fee? | SGD 61,500 | The walk skips the newest document, which is silent |
| 2 | What is the minimum spend commitment? | SGD 120,000 | Inheritance through two amendments that never mention it |
| 3 | Can either party terminate for convenience? | No. The Second Amendment deleted that clause | A right that existed and was removed |
| 4 | How long is the initial term? | Five years | The walk lands on the newest document this time |
| 5 | What is the cap on liability, and did it change? | SGD 400,000, raised from SGD 250,000 | Current value plus its history |
| 6 | What law governs the agreement and where are disputes heard? | Singapore law, courts of Singapore | An ordinary single-clause lookup |
| 7 | What do the licence fee and the minimum commitment add up to? | SGD 181,500 | Arithmetic, which must be exact rather than estimated |
| 8 | What is the data retention period? | Not in the documents | Abstention. Nothing here covers data retention |
| 9 | Is there a service credit or uptime regime? | Not in the documents | Abstention on a plausible-sounding thing that is absent |
| 10 | What is the internal matter number? | Not stated in the contract | A field whose value comes from outside the document by design |

Questions 8 and 9 are the important ones. A system that invents a plausible
answer to either has failed, however good it looks on the other eight.

## What actually happens

These numbers are from real runs, not a claim. The corpus was loaded through
the Admin tab exactly as described above, and the questions were asked at the
live chat. Your run will differ, in both directions: model output is not
deterministic, and the model you point at is not the one used here.

Two runs are recorded below because the difference between them is the point.

**The second run answered all ten correctly**, including both abstentions and
the exact SGD 181,500 arithmetic. **The first run got seven.** Same documents,
same questions, same code. What moved was extraction, not the walk.

The walk itself was right in both runs, and it is the part that is supposed to
be reliable. The licence fee resolved to the First Amendment, skipping a newer
document that is silent on it. The initial term resolved to the Second
Amendment. The minimum commitment inherited through two amendments that never
mention it. That behaviour is deterministic code, not model output.

What moves between runs is which fields the model fills in from a clause that
is about something else. The Second Amendment is where this happens, because it
deletes a clause and rewrites a number, and those sentences read like answers
to questions the document is not addressing. In the first run it put the term's
"five (5) years" into Minimum Commitment, which superseded the real SGD 120,000
and made question 7 try to add a duration to a fee. In the second run it left
Minimum Commitment alone and instead filled Survival Period from the sentence
deleting the termination clause, and answered Audit Right from the same place.
Neither run misread the same field.

Every one of those is fixed the same way and takes about a second, because the
clause the value came from is highlighted beside it and is visibly the wrong
one. Correct the field to `Not Stated` and the current value falls straight
back to the base agreement. That is [Getting
started](../docs/getting-started.md) step 9.

This is what the design expects. Extraction is allowed to misread, because
every field is checked by a person who can see the evidence, and because one
correction fixes every answer that depended on that field.

## Regenerating the PDFs

You do not need to. They are committed, and running the walkthrough only needs
the PDFs.

The generator exists so the corpus is readable, reviewable text rather than
three opaque binaries, which matters because every expected answer above is a
claim about the wording in it.

```bash
pip install reportlab
python examples/make_corpus.py
```

If you change the wording, change the tables on this page in the same commit.

## Using your own documents instead

The corpus is only useful for the domain that ships with this repository. For
your own kind of document, write your own schema first: see
[Domains](../docs/domains.md). Then build a small corpus like this one, with
the same property, which is that answering correctly requires more than
reading one document.
