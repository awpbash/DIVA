# Troubleshooting

Start with the read-only configuration check:

```bash
python -m scripts.setup --check
```

It checks the active domain, model settings, storage, and knowledge-store
connection without changing data.

## Startup

### The setup check stops at the knowledge store

Start the local emulator and wait for it to become healthy:

```bash
docker compose up -d cosmos
docker compose ps
```

Check `COSMOS_URI` and `COSMOS_KEY` in `.env`. From inside the app container,
the database host is the Compose service name, not `localhost`.

### The app asks for a domain

Set `VERBATIM_DOMAIN` in `.env`, or set `domain` in
`configs/pipeline.yaml`. If several packs exist and none is selected, DIVA stops
so documents cannot be processed against the wrong field schema.

### Every request returns 401 or 403

| Check | Meaning |
| --- | --- |
| `CHAT_API_KEY` | If set, send it in `X-API-Key` |
| Session account role | `default` is limited to chat; higher areas need more access |
| Verifier flag | Review requires `python -m scripts.accounts verifier <email> --on` |
| Account address | Only existing accounts can sign in; inspect them with `python -m scripts.accounts list` |

## Database and stored data

### The emulator data disappeared after a restart

The development emulator is disposable. Recreate it and rebuild its projection:

```bash
docker compose up -d --force-recreate cosmos
python -m scripts.rebuild_kb
```

The rebuild uses `storage/` and does not call a model.

### Documents are loaded but queries return nothing

Check the active domain and run the cross-layer audit:

```bash
python -m scripts.reconcile_kb
```

Changing `VERBATIM_DOMAIN` does not re-extract documents that were processed
under a different schema.

## Documents and extraction

### A document has no fields

Check these files in order:

1. `storage/raw/` contains the source PDF.
2. `storage/pages/` contains rendered pages.
3. `storage/pages_md/` contains reader output.
4. `storage/fields/` contains the field extraction.

If the reader output is missing, check the selected `READER`. The local path
uses RapidOCR and the optional `cu` path needs `CU_ENDPOINT` and `CU_KEY`.

### I changed the schema but the result did not change

Field results are cached by document. Re-run the field extractor with `--force`
and rebuild the knowledge projection:

```bash
python -m pipeline.kb.field_llm --all --force
python -m scripts.rebuild_kb
```

### A citation opens the right page but highlights the wrong text

Re-anchor evidence from the stored blocks and use a new chat thread:

```bash
python -m scripts.reanchor_evidence
python -m scripts.rebuild_kb
```

Existing threads keep the citation data that was stored when they were created.

### Table highlights are too broad or missing

The document may not have completed validation or table repair. Re-run the
document extraction while its cached page output is available, then rebuild the
knowledge projection. See [Pipeline overview](PIPELINE_OVERVIEW.md) for the
`doc_geometry` and table-row artifacts.

### An amendment fills a field it does not change

Open the field in **Review** and inspect the evidence. If the amendment is
silent, correct the value to `Not Stated`; the family walk can then use the last
document that established the field. Improve the field hint when the same
confusion appears across documents.

### A correction is waiting for another reviewer

Corrected values use `REVIEW_CORRECTION_APPROVALS`, which defaults to `2`. A
single-operator instance can set it to `1` and recreate the app container:

```bash
docker compose up -d --force-recreate app
```

## Application and frontend

### A frontend change is not visible

The frontend bundle is built into the image:

```bash
docker compose build app
docker compose up -d app
```

### A streamed answer arrives all at once

Disable buffering in the reverse proxy and allow a long read timeout. The
production example in [Deployment](DEPLOYMENT.md) contains the relevant proxy
settings.

### A real Cosmos account is much slower than the emulator

Avoid fetching embedding-heavy records with `SELECT *`, and batch independent
queries. The emulator hides network round trips that are visible on a remote
account.

## Windows and YAML

| Situation | Fix |
| --- | --- |
| Script output has encoding errors | Set `PYTHONUTF8=1` |
| A script cannot find its package imports | Run it as a module, such as `python -m scripts.rebuild_kb` |
| SQLite reports it is locked while the app is running | Stop the app or use `docker compose run --rm app ...` for the one-off command |
| `Yes` or `No` does not match a configured value | Quote it in YAML: `'Yes'` or `'No'` |
| A flow-mapping hint loses text after a comma | Quote the complete string |
| A Cosmos query fails on `AS value` | Use another alias such as `field_value`; `VALUE` is reserved |

## Still stuck?

Open an issue with the DIVA version, the command you ran, the output of
`python -m scripts.setup --check`, and a small synthetic reproduction. Do not
include API keys, private documents, or customer data. For vulnerabilities, use
the private process in [SECURITY.md](../SECURITY.md).
