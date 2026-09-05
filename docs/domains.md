# Define a document domain

DIVA keeps the reusable document intelligence engine separate from the
vocabulary of a particular document collection. A domain describes its
document types, graph structure, field schema, and evidence rules through
configuration, so contributors can adapt the application without changing
the core engine.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagrams/domain-layers-dark.svg">
  <img alt="Four configuration file cards across the top, labelled you write. Below a dashed line marked the line between configuration and code, a single dark slab labelled the engine, identical for every domain, listing the module names a domain author never edits." src="diagrams/domain-layers-light.svg">
</picture>

A worked domain ships in the repository. `commercial_agreement` covers
licence, supply, and non-disclosure agreements in 22 fields, and is a useful
starting point for a new domain.

There are two good ways to begin:

| Starting point | Use it when |
| --- | --- |
| **Setup wizard** | You want DIVA to draft a first field list from a description, then refine it in the browser |
| **Configuration files** | You want to understand or control the analyzer, ontology, graph mappings, and field schema directly |

The wizard can also enable document-chain tracking and cross-document party
matching. This page explains the files it creates and the additional options
available when a domain needs more detailed behavior.

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

`field_roles` connects the document-family model to the fields in your schema.
It is worth checking carefully because an incomplete mapping can leave a
relationship unresolved without producing an obvious configuration error.

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

The mapping determines how DIVA orders and connects the documents in a family.
`python -m scripts.setup --check` reports a role pointing at a field your view
does not declare, while a role naming a field that does not exist fails the
configuration build directly.

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
| `configs/policy/sensitivity.<domain>.yaml` | Which fact labels count as confidential, and which roles are denied them | The fallback file names no labels, so define this file when your domain uses restricted fields. `setup --check` reports a class naming a label your pack does not declare |
| `configs/sections.yaml` | The heading grammar used to split documents into sections | The built-in legal-drafting patterns are used, which suit contracts and may not suit your genre |

The prompt file resolves the same way everywhere: `<name>.<domain>.md` if you
wrote one, otherwise the generic `<name>.md`. The chat prompts follow the same
rule for `chat_planner`, `chat_agent`, and `chat_synth`. An override replaces
the selected prompt as a whole, so domain-specific guidance stays together and
can be reviewed as one contribution.

## 08 · Validate the domain before processing documents

Run these local checks before processing a larger document set.

```bash
python -m scripts.setup --check      # does the domain resolve and compile
python -m pytest tests/ -q           # the contract tests run over every shipped domain
python -m scripts.render_ontology    # a readable page of the graph you just declared
```

The contract tests are parametrised over every domain your checkout ships.
Adding a domain therefore puts it under the same checks as the shipped example.
They catch mismatches such as a field pointing at a label the pack does not
declare, or a party role being spelled differently in two files, before model
processing begins.

Then ingest one representative document and inspect it in the Review tab. A
small real example gives useful feedback about a schema before the domain is
applied to a larger collection.

## 09 · Authoring guidelines

| | |
| --- | --- |
| **Prefer reusable configuration.** | If a document needs its own rule, consider whether the schema or pack needs a field or mapping that can serve the wider domain |
| **Extend shared configuration.** | Inherit `_universal` and `_base`, then declare only the differences that belong to your domain |
| **Quote bare `Yes` and `No` in YAML.** | Unquoted, they parse as booleans and your enum may not match the intended values |
| **Define blank-value behavior.** | A blank can mean "not stated" in a base document and "unchanged" in an amendment, so document-family policy should make that distinction explicit |
| **Re-extract after schema changes.** | The field cache is presence-based. Run extraction with `force=True` after changing fields so the results reflect the new schema |

## 10 · Two shipped domains, and why there are two

The repository ships `commercial_agreement` as its worked example. The
project it was extracted from runs a different domain entirely, with a
physical equipment tier, per-unit tariffs, technical measurements, and a
completely different party vocabulary.

The two domains are deliberately different in shape rather than only in
naming. Together they give contributors a concrete way to verify that shared
engine changes remain adaptable across document families.

## Further reading

| | |
| --- | --- |
| The config tree in detail | [configs/README.md](../configs/README.md) |
| Pack format reference | [configs/packs/README.md](../configs/packs/README.md) |
| Analyzer format reference | [configs/analyzers/README.md](../configs/analyzers/README.md) |
| What the graph ends up looking like | [Data model](data_model.md) |
| Why schema-first at all | [Concepts](concepts.md#schema-first) |
