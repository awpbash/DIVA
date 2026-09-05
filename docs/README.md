# DIVA documentation

DIVA's documentation is organized around the way people use and extend the
application. Choose the path that matches your next task, then use the deeper
guides when you are ready to customize or contribute.

## Start here

| If you want to | Read |
| --- | --- |
| Install DIVA and ask a question | [Getting started](getting-started.md) |
| Understand the design | [Concepts](concepts.md) |
| Connect DIVA to your document domain | [Domains](domains.md) |
| Deploy and operate an instance | [Deployment](DEPLOYMENT.md) · [Troubleshooting](troubleshooting.md) |
| Look up a setting, command, or endpoint | [Reference](reference.md) |
| Explore the larger example corpus | [Real-world corpus](../examples/real_world/README.md) |

## Understand the system

| Guide | Covers |
| --- | --- |
| [Architecture](architecture.md) | How the browser, API, ingestion pipeline, model endpoint, storage, and knowledge store fit together |
| [Pipeline overview](PIPELINE_OVERVIEW.md) | The ingestion stages, their artifacts, and how they can be resumed |
| [Data model](data_model.md) | Records, edges, evidence, and vectors as they are stored |
| [Retrieval](retrieval.md) | How a question becomes tool calls, evidence, and a cited answer |
| [Tech stack](tech_stack.md) | The main dependencies and the role each one plays |

## Contribute to the project

| Resource | Use it for |
| --- | --- |
| [Contributing](../CONTRIBUTING.md) | Local setup, pull requests, domain packs, tests, and documentation changes |
| [Code of Conduct](../CODE_OF_CONDUCT.md) | Shared expectations for an open and welcoming community |
| [Security policy](../SECURITY.md) | Private reporting of security vulnerabilities |
| [Changelog](../CHANGELOG.md) | A record of notable project changes |

## Figures and screenshots

The diagrams in these pages are generated rather than drawn by hand. Their
source code and regeneration steps are in [diagrams/README.md](diagrams/README.md).
When a code or documentation change affects what a figure describes, update
the figure in the same contribution.

The screenshots use the synthetic sample corpus, so the walkthrough can be
reproduced from a fresh checkout. The capture instructions are in
[images/README.md](images/README.md).

## The design in three ideas

**The schema defines the target.** A model fills the field schema that a domain
author provides, while content outside that schema remains available for
review rather than silently becoming new structure. See
[Concepts](concepts.md#schema-first).

**Every value keeps its visual source.** Extracted values carry a verbatim
snippet and page rectangle, allowing a reviewer to inspect the source in
context. See [Concepts](concepts.md#evidence).

**People remain part of the workflow.** Documents become searchable as they
are processed, and verification adds an explicit human trust signal to the
knowledge that follows. See [Concepts](concepts.md#verification).

For the graph declared by a domain, run `python -m scripts.render_ontology`.
The command generates a Mermaid view from the ontology loaded by that
instance, which keeps the illustration aligned with the active configuration.
