# Contributing

DIVA is intended to be useful beyond the examples in this repository, and the
project benefits from people bringing different document domains, workflows,
and perspectives to it. Contributions are welcome across the codebase and the
documentation: domain packs, tests, examples, interface improvements,
performance work, bug reports, and ideas are all valuable.

If you are unsure where a change belongs, open an issue first and describe the
document workflow or problem you are trying to solve. That gives us a chance to
shape the approach together before you invest time in a large change.

## Before you begin

For a substantial feature or design change, please search the existing issues
and open a proposal before starting implementation. Small fixes and focused
documentation changes can go directly into a pull request. Keeping a pull
request focused makes it easier to review and easier for someone else to build
on later.

Please do not include confidential documents, API keys, or private customer
data in issues, pull requests, tests, screenshots, or logs. For a security
vulnerability, use the private process in [SECURITY.md](SECURITY.md) rather
than opening a public issue.

## Set up a development instance

```bash
git clone https://github.com/awpbash/diva.git
cd diva
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                            # then add a model API key
docker compose up -d cosmos
python -m scripts.setup
```

Use `python -m scripts.setup --check` to inspect the configuration without
changing it. The [Getting started](docs/getting-started.md) guide explains the
browser workflow, and [Reference](docs/reference.md) lists the available
commands.

## Verify a change

The continuous integration workflow runs the same checks used for review. The
backend checks are deterministic and do not require a model API key or a live
database:

```bash
python -m pytest tests/
python -m ruff check .
```

For frontend changes, install the locked dependencies and run the build and
tests from `web/`:

```bash
cd web
pnpm install --frozen-lockfile
pnpm run build
pnpm test
```

Changes to OCR, rendering, evidence geometry, or document configuration should
also be checked against a representative document. A passing unit test can
confirm the data shape, while a real page confirms that the evidence remains
easy for a person to inspect.

## Design principles

These principles describe the behavior contributors should preserve as DIVA
evolves:

1. **Keep evidence connected to the source.** Every extracted value should
   retain the source snippet and page geometry that let a person verify it.
2. **Keep extraction within the configured schema.** Content outside the field
   schema can be proposed for review, but the model should not silently create
   new fields at runtime.
3. **Put domain behavior in configuration.** If a rule is specific to a
   document family, prefer a field, ontology entry, prompt, or pack mapping so
   the behavior can be reused by the domain rather than embedded in a special
   case for one file.
4. **Keep deployments domain-specific.** The active domain is selected through
   `VERBATIM_DOMAIN` or `configs/pipeline.yaml`; application code should remain
   independent of any one document vocabulary.
5. **Update the documentation with the implementation.** When behavior,
   commands, configuration, or visuals change, update the relevant guide,
   example, or generated figure in the same pull request.

## Add a document domain

A domain is usually described through four YAML files:

| File | Purpose |
| --- | --- |
| `configs/analyzers/<domain>/analyzer.yaml` | Document categories, entity categories, and party roles |
| `configs/ontology/<domain>.yaml` | Graph labels, relationships, and identity rules |
| `configs/packs/<domain>.yaml` | The mapping from extracted facts to graph records |
| `configs/views/<domain>_ops.yaml` | The field schema and evidence mechanisms presented to reviewers |

Use `commercial_agreement` as a starting point, then run the configuration
check and contract tests for the new domain. The detailed authoring guide is
in [Domains](docs/domains.md).

## Documentation and examples

Documentation is part of the public interface. The documentation tests check
links and images reachable from [README.md](README.md) and
[docs/README.md](docs/README.md), so add new pages to one of those indexes when
they should be discoverable. The figure sources and regeneration instructions
are in [docs/diagrams/README.md](docs/diagrams/README.md), and the screenshot
workflow is in [docs/images/README.md](docs/images/README.md).

The sample corpus is a shared fixture. When changing an expected answer,
update the corpus generator or example documentation that establishes it, and
regenerate any affected PDFs or screenshots.

## Style

Match the surrounding code and keep comments focused on why a decision exists.
The linter checks correctness-oriented rules, while the project intentionally
keeps formatting choices lightweight so contributors can make focused diffs.

Thank you for helping make DIVA more useful, understandable, and adaptable.
