# Teaching it your documents

The engine has no idea what a contract is. Everything that knows about your
kind of document lives in four configuration files. Adding a document type
means writing those four files. It does not mean writing Python.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagrams/domain-layers-dark.svg">
  <img alt="Four configuration file cards across the top, labelled you write. Below a dashed line marked the line between configuration and code, a single dark slab labelled the engine, identical for every domain, listing the module names a domain author never edits." src="diagrams/domain-layers-light.svg">
</picture>

A worked domain ships in the repository. `commercial_agreement` covers
licence, supply and non-disclosure agreements in 22 fields, and it is the
thing to copy. Read it alongside this page.

**Or use the setup wizard now.** Its "Build from scratch" path takes a
one-sentence description, drafts a first field list with AI, and lets you
edit that list in the browser instead of writing YAML by hand. Two switches
turn on document-chain tracking and cross-document party matching, and it
writes all four files below for you. It is the faster way to get a working
first pass; this page is for understanding what it wrote, or for shaping a
domain the wizard's two switches do not cover.

## 01 · The four files

| File | Answers | Format |
| --- | --- | --- |
| `configs/analyzers/<domain>/analyzer.yaml` | What kinds of thing exist in this document, what roles they play, and when a PDF counts as this document type | YAML |
| `configs/ontology/<domain>.yaml` | What may exist in the graph: node labels, edge types, identity hubs | YAML |
| `configs/packs/<domain>.yaml` | The build contract. How raw extraction maps onto graph nodes, and what may be derived from what | YAML |
| `configs/views/<domain>_ops.yaml` | **The field schema.** What to capture per document, its type, and how its evidence is found | YAML |

They are not four views of one thing. Each constrains the next:

```
analyzer      what the reader is allowed to notice
   ↓
ontology      what may exist in the graph at all
   ↓
pack          how a noticed thing becomes a graph node, and what follows from it
   ↓
ops view      which of those become named fields a human verifies
```

Anything the analyzer never notices cannot reach the graph. Anything the pack
does not map is quarantined rather than guessed. Anything not in the ops view
is searchable text but not a verified field.

## 02 · Start from the questions, not the documents

Write the ops view first, even though it is the last file in the chain.

The temptation is to start by cataloguing everything in your documents. That
produces a schema of two hundred fields nobody verifies. Start instead from
the ten questions people actually ask, work back to the values that answer
them, and make those the fields. You can add more later, and the review
workflow will tell you which ones were worth it.

A field earns its place if a wrong answer to it would cost somebody something.

## 03 · The analyzer

Declares what the reader is allowed to notice, and when a document is yours.

```yaml
id: commercial_agreement
version: 1.0
extends: _universal

classify_when:
  any:
    - "doctype matches '(license|licence|distribution|supply)[ _]agreement'"
    - "title contains 'License Agreement'"

categories:
  - organization
  - person
  - money
  - date
  - obligation
  - right
  - warranty          # this domain's own concept, not in the universal set

roles:
  organization: [disclosing_party, receiving_party, licensor, licensee]
  person:       [signatory, notice_recipient]
  money:        [license_fee, minimum_commitment, liability_cap]
  right:        [termination_for_convenience, audit, assignment, renewal]
```

`extends: _universal` inherits the categories every document type has. Your
file adds what is specific and narrows what is not needed. A domain with no
physical equipment simply does not list an equipment category, and the whole
equipment half of the engine stays out of its way.

Role vocabularies are per domain and there is no privileged set. A domain
whose parties are licensor and licensee is not forced into somebody else's
supplier and customer world. The party fields in the ops view are validated
against exactly this list, so a typo fails the build instead of silently
matching nothing.

## 04 · The ontology

Declares what may exist in the graph. Node labels grouped into layers, edge
types, and the identity hubs that cross-document resolution links into.

```yaml
doctype: commercial_agreement
version: 1

layers:
  document:   [Document, Agreement]
  layout:     [Section, Block]
  fact:       [Party, Location, Payment, Date, Obligation, Right, Warranty]
  provenance: [FactMention]
  evidence:   [EvidenceSpan]
  identity:   [CanonicalParty, CanonicalJurisdiction]
  quarantine: [Proposal]
```

The layers are not decorative. `evidence` is what the PDF viewer draws,
`identity` is what mentions resolve into instead of minting duplicates, and
`quarantine` is where unmapped content goes so it is reviewable rather than
lost.

The layers are not the whole file. An `identity:` block sits alongside them and
drives three things that are easy to miss:

```yaml
identity:
  canonical_roles: [disclosing_party, receiving_party, licensor, licensee]
  legal_suffixes: [inc, corp, llc, ltd, limited, plc, gmbh, pte]
  generic_tokens: [confidential, disclosure, licence]
```

`canonical_roles` names the PRINCIPAL parties, the ones a document is between,
as opposed to the signatories and notice contacts it also names. Question
scoping and the corpus catalog use it, and so does the guard that refuses to
mint a canonical entity from a bare role word: without your roles listed here,
"the Receiving Party" gets its own entity and real counterparties resolve onto
it.

`legal_suffixes` are peeled when matching entity names, so one company written
three ways is one node.

`generic_tokens` are the words your corpus uses so often that they identify
nothing. The retrieval scoper drops them before matching a question against
entity names. Note how little two domains share here: a stopword set is domain
vocabulary, which is exactly why it cannot live in the engine.

The analyzer's category list and this file's fact labels have to agree. A test
enforces it, because a mismatch means content is extracted and then has
nowhere to live.

## 05 · The pack

The build contract, and the only file where the mapping from messy reality to
clean structure lives.

```yaml
doctype: commercial_agreement
extends: _base

fact_types:
  organization:
    label: Party
    key: party_id
    raw_labels: [name-or-org, organization, company]
    shape: '{"name": "<full legal name>", "notice_address": "<address or null>"}'
    bundling: 'Bundle stubs referring to the SAME organization across pages.'
    properties: [party_id, doc_id, name, normalized_name, role, address]
    filterable: [entity_type, role, normalized_name, name]
```

`raw_labels` is the piece that does the real work. Models return "name-or-org"
one day and "organization" the next, and this is where that variance is
absorbed once, declaratively, rather than by a dozen conditionals scattered
through the loader.

The set is closed. Anything a model returns that is not mapped here is
quarantined as a proposal for review. It is never invented into the graph and
it is never dropped.

`_base.yaml` holds everything that is true of every document type. Your pack
declares only what is specific, and inherits the rest.

## 06 · The ops view: the field schema

This is the important one. It is the canonical extraction target, the thing
the reviewer sees, and the thing the scorer grades.

```yaml
view: commercial_agreement_ops
extends_pack: commercial_agreement
version: 1

enums:
  yes_no: ["Yes", "No", "Not Stated"]     # quote these: bare Yes/No is a boolean

categories:
  commercial_terms:
    title: Commercial Terms
    fields:
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

### Value types

| `type` | For |
| --- | --- |
| `text` | A short string |
| `number` | A bare number |
| `value` | A quantity with a unit or currency, kept as a structured amount so it can be summed |
| `enum` | One of a closed list, declared under `enums` and referenced by `values_ref` |
| `presence_enum` | Whether something is present at all, usually Yes / No / Not Stated |
| `free_text` | A verbatim span, for clauses you want quoted rather than parsed |
| `reference` | An identifier that points at something outside the document |
| `equipment` | A named piece of physical plant, for domains that have any |

### Evidence mechanisms

`source.mechanism` says how a field's value and its evidence are found. This
is what keeps every value anchored.

| Mechanism | How the value is found |
| --- | --- |
| `value` | A monetary or quantitative fact of a given label, matched on parameter name |
| `party` | A party playing a named role, from the recitals or the signature block |
| `enum` | A fact of a given label, mapped through `role_map` onto your closed vocabulary |
| `presence_enum` | Whether any matching clause exists at all, keyed on keywords |
| `free_text` | The verbatim span of a clause, keyed on keywords |
| `equipment` | A named equipment item, for domains that declare one |
| `recital` | Filled by the model from the document's opening, including the amendment chain it declares |
| `llm` | Filled by the model from the field's own description. The mechanism for fields added through the schema editor at runtime |
| `external` | Not in the document at all. Operational metadata a person or another system supplies |

The first six read structured facts the pipeline already built, so they carry
their evidence automatically. `recital` and `llm` are filled directly by the
model, which anchors them as it extracts. `external` has no evidence by
definition and is shown as such.

### The document family policy

This block is what makes the supersedence walk work for your vocabulary
instead of somebody else's.

```yaml
document_family:
  document_types: [Agreement, Amendment, Assignment, Side Letter, Notice, Other]
  relations:
    amendment: AMENDS
    amended and restated: SUPERSEDES
    assignment: NOVATES
  field_roles:
    document_type: document_type
    ordinal: amendment_ordinal
    relationship_type: document_type
    document_date: agreement_date
    amends_dated: amends_document
    effective_date: effective_date
  ancillary_types: [exhibit, side letter, ancillary]
```

`document_types` is the menu the upload form offers and the only values intake
accepts. Name what your corpus actually contains. Declare none and a generic
set is used.

`relations` maps the words your documents use onto the graph edges. If your
world calls it a "variation" rather than an "amendment", say so here.

`field_roles` is the one most people miss, and getting it wrong fails quietly.

The chain walk needs six things from every document: what kind of document it
is, its position in a sequence, which word declares the relationship, the date
it is dated, which earlier document it changes, and when the change takes
effect. Those six ROLES are fixed. The field each one reads is not, because
your schema names its fields whatever you named them. One domain calls the date
`agreement_date` and another calls it `document_date`, and both are right.

Map every role to a field key inside your `document_relationships` category. In
the example above, `relationship_type` maps to `document_type` because this
domain declares the relationship by what it CALLS the document, matching the
`relations` keys above it.

Get this wrong and nothing errors. The lookup returns nothing, every document
ties on an undated sentinel, and "the latest document wins" quietly becomes
"the last document id alphabetically wins". That looks exactly like a corpus
with no amendments in it. `python -m scripts.setup --check` reports any role
pointing at a field your view does not declare, and a role naming a field that
does not exist fails the config build outright.

`ancillary_types` names document kinds that state values but are never a link
in the chain, so an exhibit never becomes the base that an amendment resolves
against.

Per category, `supersedes: false` excludes it from the walk. Use it for facts
about the document itself and for stable party identity, which a later
document must not overwrite:

```yaml
parties:
  title: Parties
  supersedes: false      # counterparty identity, not an evolving term
```

### Sensitivity

Field values can be classified, and the classification is enforced on the
server before anything is sent to a browser:

```yaml
sensitivity:
  default_level: general
  categories:
    commercial_terms: confidential
    parties: confidential
  field_overrides:
    parties.signatory_name: general     # it is on the public execution page
```

<a id="activate"></a>

## 07 · Activate it

One deployment serves one domain. Declare it:

```bash
VERBATIM_DOMAIN=commercial_agreement
```

or in `configs/pipeline.yaml`:

```yaml
domain: commercial_agreement
```

If exactly one pack is present under `configs/packs/`, it is detected and you
need neither. With more than one pack and no declaration, the app refuses to
start rather than guess, because guessing means extracting your whole corpus
against the wrong schema.

### Three more files your domain may want

The four files above are the required set. Three optional ones change behaviour
if you write them, and are worth knowing about because two of them fail quietly
when they are missing or wrong.

| File | What it does | If you skip it |
| --- | --- | --- |
| `configs/prompts/field_extract.<domain>.md` | The extraction persona: the guidance that tells the model what your documents look like and how to read them | A generic persona is used. It works, and it is less accurate than one written for your corpus |
| `configs/policy/sensitivity.<domain>.yaml` | Which fact labels count as confidential, and which roles are denied them | **Redaction does nothing.** The fallback file names no labels on purpose, because a wrong label list looks like protection and provides none. `setup --check` reports a class naming a label your pack does not declare |
| `configs/sections.yaml` | The heading grammar used to split documents into sections | The built-in legal-drafting patterns are used, which suit contracts and may not suit your genre |

The prompt file resolves the same way everywhere: `<name>.<domain>.md` if you
wrote one, otherwise the generic `<name>.md`. The chat prompts follow the same
rule, as `chat_planner`, `chat_agent` and `chat_synth`. Overriding one replaces
it wholesale, deliberately: filling your nouns into a shared prompt produces a
prompt that is worse in every domain.

## 08 · Check it before you spend anything

Three free checks, in the order worth running them.

```bash
python -m scripts.setup --check      # does the domain resolve and compile
python -m pytest tests/ -q           # the contract tests run over every shipped domain
python -m scripts.render_ontology    # a readable page of the graph you just declared
```

The contract tests are the ones that matter here. `tests/extraction/
test_pack_contract.py`, `tests/kb/test_opsview_contract.py` and
`tests/extraction/test_ontology_contract.py` are parametrised over every
domain your checkout ships, so adding your files puts them under the same
gates as the shipped example. They will tell you, before you pay for a single
model call, that a field points at a label the pack never declares, or that a
party role is spelled differently in two files.

Then ingest one document and look at the result in the Review tab. One
document tells you more about a schema than another day of writing YAML.

## 09 · Rules that hold whatever you are modelling

| | |
| --- | --- |
| **No per-document special cases.** | If a document needs its own rule, the schema is missing a field or the pack is missing a mapping. A heuristic for one difficult document is how a system stops scaling |
| **Extend, do not fork.** | Inherit `_universal` and `_base` and declare only your differences. A forked base drifts and stops receiving fixes |
| **Quote bare `Yes` and `No` in YAML.** | Unquoted, they parse as booleans and your enum silently stops matching |
| **A blank is not nothing.** | It means "not stated" in a base document and "unchanged" in an amendment. Get the document family policy right or every current value is wrong |
| **New fields need a re-extraction.** | The field cache is presence-based. After a schema change, re-run extraction with `force=True` or you will grade stale results and believe them |

## 10 · Two shipped domains, and why there are two

The repository ships `commercial_agreement` as its worked example. The
project it was extracted from runs a different domain entirely, with a
physical equipment tier, per-unit tariffs, technical measurements, and a
completely different party vocabulary.

That is not an accident of history, it is the test. The two domains are
deliberately different in shape rather than just in naming, so "this is
configurable" is a claim the test suite checks on every commit rather than a
sentence in a README. If a change quietly hardcodes one domain's assumptions
into the engine, the other domain stops compiling.

## Further reading

| | |
| --- | --- |
| The config tree in detail | [configs/README.md](../configs/README.md) |
| Pack format reference | [configs/packs/README.md](../configs/packs/README.md) |
| Analyzer format reference | [configs/analyzers/README.md](../configs/analyzers/README.md) |
| What the graph ends up looking like | [Data model](data_model.md) |
| Why schema-first at all | [Concepts](concepts.md#schema-first) |
