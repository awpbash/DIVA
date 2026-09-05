# Documentation

Start where your question is.

| I want to | Read |
| --- | --- |
| Get it running and ask a real question | [Getting started](getting-started.md) |
| Understand why it is built this way | [Concepts](concepts.md) |
| Point it at my own kind of document | [Domains](domains.md) |
| Know what talks to what | [Architecture](architecture.md) |
| Look up a setting, a command, or an endpoint | [Reference](reference.md) |
| Fix something that is broken | [Troubleshooting](troubleshooting.md) |
| Put it on a server | [Deployment](DEPLOYMENT.md) |
| Test it against real contracts, not just the demo | [Real-world corpus](../examples/real_world/README.md) |

Deeper material, written for people changing the engine rather than using it:

| Page | Covers |
| --- | --- |
| [Pipeline overview](PIPELINE_OVERVIEW.md) | Every ingestion stage, what it writes, what it costs |
| [Data model](data_model.md) | Records, edges, and vectors as they are stored |
| [Retrieval](retrieval.md) | How a question becomes tool calls and citations |
| [Tech stack](tech_stack.md) | Dependencies and why each one is there |

For the graph your own domain declares, run `python -m scripts.render_ontology`.
It draws the node labels and edge types as Mermaid, from the ontology this
instance actually loaded, so it is never out of date the way a checked-in copy
would be.

## The three things worth knowing before anything else

**The schema is the target.** A model does not decide what matters. You write
a field schema, the model fills it in, and anything outside the schema is
quarantined rather than invented. See [Concepts](concepts.md#schema-first).

**Every value is anchored.** A value carries the verbatim snippet it came from
and the rectangle on the page where that snippet sits. If the anchor is wrong,
the value is worthless, no matter how right it looks. See
[Concepts](concepts.md#evidence).

**A person signs the values.** Extraction is allowed to be imperfect because
it is reviewed. Review does not block anything: a document is searchable the
moment it lands, and verification upgrades its trust afterwards. See
[Concepts](concepts.md#verification).

## Reading the code

| Directory | Its own README |
| --- | --- |
| `pipeline/` | [pipeline/README.md](../pipeline/README.md) |
| `api/` | [api/README.md](../api/README.md) |
| `web/` | [web/README.md](../web/README.md) |
| `configs/` | [configs/README.md](../configs/README.md) |
| `tests/` | [tests/README.md](../tests/README.md) |

## Figures and screenshots

The diagrams in these pages are generated, not drawn. The source and the
regeneration steps are in [diagrams/README.md](diagrams/README.md). If you
change something a figure describes, change the figure in the same commit.

The screenshots are all taken against the synthetic sample corpus, so you can
reproduce any of them. The rule and the recapture steps are in
[images/README.md](images/README.md).
