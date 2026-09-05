# `configs/`

The `configs/` tree describes what a DIVA instance extracts and how it stores
the result. It is the place to change document-specific behavior; the reusable
engine lives in `pipeline/` and `api/`.

For the guided authoring path, see [Define a document domain](../docs/domains.md).

## Directory map

| Directory | Contains |
| --- | --- |
| `analyzers/` | Document categories and party roles |
| `ontology/` | Allowed record labels, edge types, and identity rules |
| `packs/` | Mapping from extracted facts to stored records and derived links |
| `views/` | The field schema shown in Review and used by schema-first extraction |
| `prompts/` | Markdown prompts for reading, extraction, chat, and table repair |
| `policy/` | Sensitivity and role rules |
| `schemas/` | JSON Schemas that validate configuration files |
| `pipeline.yaml` | Shared rendering, confidence, domain, and snippet settings |

## How a domain is composed

```text
analyzer + ontology + pack + view + prompts
                    │
                    ▼
            shared pipeline and API
```

The active domain is selected by `VERBATIM_DOMAIN`, by `domain` in
`pipeline.yaml`, or by autodetection when exactly one pack exists.

## Add a domain

Create the required files under the existing directory structure:

```text
configs/
├── analyzers/<domain>/analyzer.yaml
├── ontology/<domain>.yaml
├── packs/<domain>.yaml
└── views/<domain>_ops.yaml
```

Start from `commercial_agreement` and extend `_universal` and `_base` where
possible. Keep shared rules in the shared files and put domain-specific names
in the new files.

Run the checks before processing documents:

```bash
python -m pipeline.extraction.lint
python -m scripts.setup --check
python -m pytest tests/ -q
```

The loader checks references between the files, the pack compiler checks its
record mappings, and the tests check the contract against the code that reads
it.

## Editing rules

- Quote `Yes` and `No` when they are string values.
- Quote flow-mapping strings that contain commas.
- Keep field keys stable once documents have been processed.
- Re-run field extraction after changing a view or extraction prompt.
- Treat `Not Stated` as an intentional result, not an empty placeholder.
- Add a small test or example when a new field or relationship changes behavior.

The detailed formats are documented beside their loaders:

- [Analyzers](analyzers/README.md)
- [Packs](packs/README.md)
- [Ontology](ontology/README.md)
- [Prompts](prompts/README.md)
- [Schemas](schemas/README.md)
