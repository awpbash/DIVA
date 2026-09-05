# Concepts

This guide explains the design decisions behind DIVA. Read it when you are
evaluating the application, defining a document domain, or preparing a change
that should fit the existing architecture.

## 01 · The problem with the obvious approach

Passage retrieval is useful for questions such as "what does this clause say?"
A document archive also needs to answer questions that depend on document
relationships, complete collections, structured values, and source context.
DIVA adds those layers instead of asking a language model to infer them from a
small group of retrieved passages.

| The question | What the archive needs |
| --- | --- |
| "What is the current fee?" | The value must be resolved across the amendment chain that establishes it |
| "What do the deposits add up to?" | Every relevant structured value must be included before the total is calculated |
| "Which sites have a renewal right?" | The query must cover the complete document collection |
| "Is there a service credit regime?" | The system needs an explicit way to represent that the documents do not state one |
| "Where does that number come from?" | The answer needs a source passage and a location that a person can inspect |

These requirements are especially important when an archive supports decisions
about money, obligations, or rights. DIVA therefore treats evidence and review
as part of retrieval rather than as separate concerns.

<a id="schema-first"></a>

## 02 · Schema-first extraction

Instead of mining open-ended text, a model fills in a field schema you wrote.

```yaml
license_fee:
  title: Licence Fee
  type: value
  multiplicity: 1
  hint: "The fee payable for the licence or subscription."
  source:
    mechanism: value
    label: Payment
    match: {parameter_any: [license_fee, licence_fee, subscription_fee]}
```

If a model names the things it finds, the same concept can return as "annual
licence fee", "yearly subscription charge", "the Fee", or "licensing cost"
across four documents. A named field with a bounded type gives the domain a
shared vocabulary, so values can be reviewed and compared consistently.

Three principles follow from this approach:

- **The schema is the only target.** Content that does not map to a field is
  quarantined for review. It is never silently dropped and it is never
  silently added to the schema at runtime.
- **"Not Stated" is a valid value.** Abstaining is a correct outcome, and the
  scorer counts a correct abstention as a success rather than a miss.
- **The model extracts, it does not decide.** New field definitions are
  proposed to a human, never self-registered.

The full authoring guide is in [Domains](domains.md).

<a id="evidence"></a>

## 03 · Evidence anchoring

Every value carries the verbatim snippet it came from and the rectangle on the
page where that snippet sits.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagrams/evidence-chain-dark.svg">
  <img alt="A contract page with two highlighted clauses. Curved threads join each highlight to a named field holding the value, the verbatim snippet, and the page rectangle. A third field reads Not Stated and has no thread, because there is nothing on the page to anchor it to." src="diagrams/evidence-chain-light.svg">
</picture>

This is a practical part of the design. A reviewer approving a field needs to
see the clause it came from, on the page and in context. A precise highlight
gives the reviewer a direct way to confirm the relationship between the value
and its source before the value is used downstream.

For that reason, bounding-box fidelity is treated as a core quality criterion
alongside new features.

The third row in the figure says "Not Stated" and has no thread because there
is no source passage to point at. An explicit abstention lets reviewers and
downstream queries distinguish an unmentioned field from an unavailable value.

<a id="verification"></a>

## 04 · Human verification

DIVA combines model-assisted extraction with human verification. This gives
teams a practical way to handle varied documents while keeping an explicit
review step for values that need a decision.

Four properties make review survivable at scale:

| | |
| --- | --- |
| **Never blocking** | A document is searchable as soon as processing finishes, while verification adds a trust signal afterwards |
| **Triaged** | Uncertain and high-value fields surface first, directing reviewer attention where it has the greatest effect |
| **Corrections generalise** | A correction can become a definition, alias, or rule that benefits later documents in the same domain |
| **Voted** | Approved verifiers can review the same value and resolve it through the configured approval workflow |

Verified values carry a trust tier and lineage, so an answer can say who
certified the number and when.

## 05 · Aligned knowledge

Because every document fills the same verified fields, cross-document questions
can be answered through a shared knowledge model. Three deterministic layers
support that model, keeping entity alignment, document relationships, and field
resolution explicit.

### Entity resolution links mentions to canonical parties

A mention of a party in a document is linked to a canonical hub through a
multi-signal match on name, context and identifiers. When the signals are not
strong enough it abstains and flags for review rather than creating a new hub.

This keeps variants such as "Fairhaven Systems Limited", "Fairhaven Systems
Ltd", and "Fairhaven Systems" connected to the same canonical party, allowing
cross-document queries to use the identity information consistently.

### The document family is declared, not inferred

Contracts say what they amend, in their own recitals:

> THIS SECOND AMENDMENT is made on 20 January 2025 to the Master Software
> License Agreement dated 14 March 2023 between Northwind Logistics Pte. Ltd.
> and Fairhaven Systems Limited, as amended by the First Amendment dated
> 2 September 2024.

The chain is extracted and verified as ordinary fields. DIVA uses the
relationships declared in the documents rather than inferring them from
filenames, dates, or folder structure, and the relationship fields receive the
same evidence-focused review as other important fields.

### "Current" is resolved per field

The latest document in the chain that sets a field wins. Fields nobody re-sets
inherit from the parent.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagrams/supersedence.gif">
  <img alt="Two fields resolved separately by walking backwards along a chain of three documents. The licence fee is found in the first amendment, the initial term in the second." src="diagrams/supersedence.gif">
</picture>

The meaning of a blank depends on where the document sits in the chain:

| A blank in | Means |
| --- | --- |
| A base agreement | Not stated. The contract is silent on this |
| An amendment | Unchanged. Inherit whatever the parent said |

The same absence can therefore carry two meanings. DIVA resolves this rule in
application code so that "current" values follow the document family and the
field history remains visible.

On screen the result is one row per field: the value in force, the document
that set it, and everything it replaced still legible underneath.

![Three money fields from the sample corpus. Each shows the current value with the document that set it, and the earlier value struck through below.](images/knowledge-supersedence.png)

## 06 · Two retrieval modes

The question determines which retrieval path is most useful.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagrams/retrieval-modes-dark.svg">
  <img alt="A question splits into two tracks drawn in different styles. On the left, semantic search narrows soft overlapping candidates to a single highlighted clause. On the right, a deterministic query lists every matching field in a ruled table and totals them below a rule, computed in Python rather than by a language model." src="diagrams/retrieval-modes-light.svg">
</picture>

**Semantic search over the verified text** supports natural-language lookups
where the answer is expressed in a relevant clause. The citation keeps the
retrieved passage available for inspection.

**Deterministic queries with exact arithmetic** support totals, comparisons, and
questions that depend on the current value in a document family. These
operations work over the aligned fields and return the records used in the
calculation.

Sums are computed in Python, while the model selects tools and presents the
result. This keeps arithmetic, field selection, and document history separate
from answer phrasing.

Canonicalisation is narrow on purpose. Only the dimensions the structured
operations join and aggregate on get canonicalised. Everything else stays as
text, where semantic search handles it perfectly well.

## 07 - Evaluation and measurement

A deterministic scorer grades extraction field by field against a
human-verified gold set:

```bash
python -m eval.extraction.score --run NAME
```

It reports extraction outcomes together with evidence integrity, giving a
domain team a repeatable way to track improvements as its verified corpus grows.

The scorer runs locally without a model call, making it suitable for a regular
development and review workflow.

## 08 - Scope and extension points

The current design focuses on schema-defined, evidence-backed document work.
That focus gives contributors clear extension points:

| Focus | Where to extend it |
| --- | --- |
| Schema-defined extraction | Add fields, analyzers, prompts, and pack mappings for a document domain |
| Evidence-backed answers | Improve readers, geometry, evidence anchoring, and citation presentation |
| Structured document knowledge | Extend the ontology, relationship model, or deterministic retrieval tools |
| Human-centered review | Improve triage, voting, correction workflows, and reviewer feedback |
| Domain-specific deployments | Run separate instances or contribute configuration patterns for new document families |

## Further reading

| | |
| --- | --- |
| How the pieces fit together | [Architecture](architecture.md) |
| Writing a schema for your own documents | [Domains](domains.md) |
| Stage-by-stage ingestion detail | [Pipeline overview](PIPELINE_OVERVIEW.md) |
| How a question becomes tool calls | [Retrieval](retrieval.md) |
