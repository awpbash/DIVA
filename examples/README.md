# Examples

The sample corpus is a small contract family for trying DIVA without using
private documents. It is synthetic: the companies, people, dates, and amounts
are invented.

## Sample files

| File | Role |
| --- | --- |
| `corpus/01-master-license-agreement-2023.pdf` | Base agreement |
| `corpus/02-first-amendment-2024.pdf` | Changes the licence fee and liability cap |
| `corpus/03-second-amendment-2025.pdf` | Changes the term and removes termination for convenience |

The sample domain is `commercial_agreement`. Upload the files from the Admin
screen in order and declare both amendments as children of the master
agreement. The declarations create the document family used by the current
value lookup.

## Expected current values

| Field | Current value | Set by |
| --- | --- | --- |
| Licence fee | SGD 61,500 per year | First Amendment |
| Minimum commitment | SGD 120,000 | Master agreement |
| Liability cap | SGD 400,000 | First Amendment |
| Initial term | Five years | Second Amendment |
| Renewal term | Twelve months, automatic | Master agreement |
| Termination for convenience | No | Second Amendment |
| Governing law | Singapore | Master agreement |

The latest document does not mention every field. DIVA finds the newest document
that states each field, so the licence fee comes from the First Amendment while
the initial term comes from the Second Amendment.

## Questions to try

| Ask | What it checks |
| --- | --- |
| What is the current annual licence fee? | Per-field current-value lookup |
| How long is the initial term? | A value changed by the newest amendment |
| What is the cap on liability, and did it change? | Current value and earlier value |
| What law governs the agreement? | A direct clause lookup |
| What do the licence fee and minimum commitment add up to? | Structured arithmetic |
| Is there a service-credit regime? | An explicit `Not Stated` result |
| What is the internal matter number? | A field absent from the contract |

Select a citation in Chat to open the source page. Open **Review** to approve a
field or correct one whose evidence does not support its value.

## Use your own documents

For another document type, define the field schema and document-family rules
first. [Define a document domain](../docs/domains.md) explains the four required
configuration files. A small representative corpus makes it easier to check
field names, blank-value behavior, and evidence rectangles before processing a
larger collection.

## Regenerate the PDFs

The committed PDFs are enough for the walkthrough. If you change the sample
wording, regenerate them with:

```bash
pip install reportlab
python examples/make_corpus.py
```

Update the expected values and questions in this file when the sample wording
changes.
