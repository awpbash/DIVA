# DIVA documentation

This index separates the public guides from the source-directory notes. Start
with the guide that matches the task in front of you. The pages link to deeper
details when those details are useful.

## Choose a guide

| If you want to… | Read |
| --- | --- |
| Install DIVA and try the sample | [Getting started](getting-started.md) |
| Understand the main design choices | [Concepts](concepts.md) |
| See the application components | [Architecture](architecture.md) |
| Follow a PDF through ingestion | [Pipeline overview](PIPELINE_OVERVIEW.md) |
| Understand chat, search, and citations | [Retrieval](retrieval.md) |
| Define a document domain | [Domains](domains.md) |
| Find settings, commands, and endpoints | [Reference](reference.md) |
| Deploy an instance | [Deployment](DEPLOYMENT.md) |
| Diagnose a running instance | [Troubleshooting](troubleshooting.md) |

## Technical references

| Page | Use it for |
| --- | --- |
| [Data model](data_model.md) | The records, edges, evidence, and vectors written by the pipeline |
| [Tech stack](tech_stack.md) | Dependency choices and runtime switches |
| [`configs/`](../configs/README.md) | Domain configuration and prompt formats |
| [`pipeline/`](../pipeline/README.md) | Code layout and maintainer notes |
| [`api/`](../api/README.md) | FastAPI routes and backend modules |
| [`tests/`](../tests/README.md) | Test layout and local verification |

The pages are intentionally split by audience. The Architecture page explains
where components run, Tech stack lists what they are built with. Pipeline
overview explains what an operator sees when a PDF is processed, the
`pipeline/` notes explain where a contributor changes that behavior. Data model
describes stored records rather than repeating the ingestion walkthrough.

## Contributing

Documentation changes should update the guide that owns the subject and any
links that lead to it. Use the synthetic corpus for screenshots and examples.
See [Contributing](../CONTRIBUTING.md) for the repository workflow.

Figures are simple, generated SVGs so they remain readable in light and dark
themes. Their source and regeneration command are in
[diagrams/README.md](diagrams/README.md). Screenshots and branding assets are
listed in [images/README.md](images/README.md).

## The short version

1. DIVA reads a PDF into text and page geometry.
2. A domain schema names the fields to extract.
3. Each value keeps its source text and page location.
4. Reviewers can verify or correct the result.
5. Chat and structured queries use the stored values and return citations.
