# Reference

Use this page for settings, commands, endpoints, and roles. The annotated
[`.env.example`](../.env.example) is the source of the setting comments.

## Settings

Only `OPENAI_API_KEY` is required for a normal local setup. The other settings
have development defaults or apply only to a selected deployment.

### Models and reader

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | None | API key for the configured model endpoint |
| `OPENAI_BASE_URL` | OpenAI API | OpenAI-compatible endpoint for chat and extraction |
| `OPENAI_TEXT_MODEL` | `gpt-5.4-mini` | General text and chat |
| `OPENAI_VISION_MODEL` | `gpt-5.4-mini` | Page correction and table repair |
| `OPENAI_REASONING_MODEL` | `gpt-5.4` | Whole-document extraction |
| `OPENAI_EMBED_MODEL` | `text-embedding-3-large` | Semantic search vectors |
| `READER` | `rapidocr` | `rapidocr` for local OCR or `cu` for Azure Content Understanding |
| `CU_ENDPOINT`, `CU_KEY`, `CU_ANALYZER` | None / `prebuilt-layout` | Azure reader settings when `READER=cu` |

### Domain and storage

| Variable | Default | Purpose |
| --- | --- | --- |
| `VERBATIM_DOMAIN` | Auto-detected when one pack exists | Active domain configuration |
| `STORAGE_ROOT` | `./storage` | Source files, app state, and pipeline artifacts |
| `COSMOS_URI` | `http://localhost:8081` | Local emulator or live Cosmos endpoint |
| `COSMOS_KEY` | Emulator key | Key for the selected Cosmos endpoint |
| `COSMOS_DB` | `verbatim` | Cosmos database name |
| `COSMOS_CONTAINER` | `kb` | Knowledge container |
| `COSMOS_VECTOR_MODE` | `client` | Exact local ranking or `native` Cosmos vector search |
| `AZURE_STORAGE_CONNECTION_STRING` | None | Optional Blob mirror |
| `AZURE_BLOB_ACCOUNT_URL`, `AZURE_BLOB_CONTAINER` | None / `storage` | Optional managed-identity Blob mirror |

### Application and access

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_NAME`, `APP_TAGLINE` | Set during setup | Branding returned by `GET /branding` |
| `APP_DOCUMENT_NOUN`, `APP_DOCUMENT_NOUN_PLURAL` | `document`, `documents` | Terms shown in the interface |
| `BOOTSTRAP_ADMIN_EMAIL` | `admin@localhost` | First account when the database has no accounts |
| `BOOTSTRAP_ADMIN_NAME` | `Administrator` | Name for that account |
| `CHAT_API_KEY` | Unset | Optional shared key in `X-API-Key` |
| `CHAT_CORS_ORIGINS` | Development defaults | Comma-separated allowed browser origins |
| `CHAT_CORS_ORIGIN_REGEX` | Development loopback regex | Replacement origin regex |
| `CHAT_DISABLED_TOOLS` | Unset | Comma-separated retrieval tools to disable |
| `LOG_FORMAT` | Human-readable | Set to `json` for one JSON object per log line |

### Processing and review

| Variable | Default | Purpose |
| --- | --- | --- |
| `RENDER_DPI` | `300` | Resolution of rendered page images |
| `VISION_PAGE_CONCURRENCY` | `4` | Pages read in parallel |
| `REVIEW_MIN_APPROVALS` | `1` | Approvals required for a normal value |
| `REVIEW_CORRECTION_APPROVALS` | `2` | Approvals required for a corrected value |
| `STORAGE_SEED_URL` | Unset | Seed an empty storage volume from a tarball |
| `STORAGE_SEED_FORCE` | `false` | Reapply a storage seed over existing content |

## Commands

Run scripts as modules. On Windows, set `PYTHONUTF8=1` first.

### Setup and accounts

| Command | Purpose |
| --- | --- |
| `python -m scripts.setup` | Create or check the local setup |
| `python -m scripts.setup --check` | Inspect configuration without changing it |
| `python -m scripts.setup --check-models` | Check the model endpoint with a small request |
| `python -m scripts.accounts list` | List accounts and roles |
| `python -m scripts.accounts add <email> --role admin` | Add an account |
| `python -m scripts.accounts role <email> <role>` | Change an account role |
| `python -m scripts.accounts verifier <email> --on` | Grant review access |
| `python -m scripts.accounts remove <email>` | Remove an account while retaining history |

### Documents and the knowledge base

| Command | Purpose |
| --- | --- |
| `python -m pipeline.ingest <pdf>` | Render and ingest one PDF |
| `python -m pipeline.ingest --all` | Ingest PDFs already in `storage/raw/` |
| `python -m pipeline.kb.field_llm --all` | Fill the active field schema for all read documents |
| `python -m pipeline.kb.field_llm --all --force` | Re-run field extraction after a schema change |
| `python -m scripts.rebuild_kb` | Rebuild the Cosmos projection without model calls |
| `python -m scripts.build_km` | Rebuild the aligned knowledge layer |
| `python -m scripts.reconcile_kb` | Run a read-only cross-layer audit |
| `python -m scripts.reanchor_evidence` | Recompute evidence rectangles from cited blocks |
| `python -m scripts.delete_doc <doc_id>` | Remove one document from stored layers |
| `python -m scripts.render_ontology` | Render the active graph vocabulary |

### Checks and evaluation

| Command | Purpose |
| --- | --- |
| `python -m pytest tests/ -q` | Run backend tests without model or database calls |
| `python -m ruff check .` | Run the Python linter |
| `cd web && pnpm run build` | Build the frontend |
| `cd web && pnpm test` | Run frontend tests |
| `python -m eval.extraction.score --run NAME` | Score a run against a gold set |
| `python -m scripts.ask "<question>"` | Ask one question against a running instance |

## HTTP API

The running application publishes the complete OpenAPI reference at
<http://localhost:8000/docs>. The routes below are the useful map.

| Area | Routes |
| --- | --- |
| Health and sign-in | `GET /healthz`, `GET /readyz`, `GET /branding`, `POST /auth/login`, `POST /auth/logout` |
| Session and chat | `GET /auth/me`, `POST /chat`, `GET /threads`, `PUT /threads`, `GET /documents` |
| Source evidence | `GET /pdf/{doc_id}`, `GET /evidence/{evidence_id}` |
| Knowledge | `GET /km/families`, `GET /km/family/{group}`, `GET /km/aggregate`, `GET /km/export`, `GET /km/page/{doc_id}/{page_no}` |
| Graph | `GET /graph/subgraph`, `GET /graph/overview`, `GET /graph/hubs` |
| Review | `GET /review/docs`, `GET /review/doc/{doc_id}`, `POST /review/doc/{doc_id}/field/{field_key}/verify` |
| Admin | `POST /admin/upload`, `POST /admin/extract/{doc_id}`, `GET /admin/jobs`, `GET /admin/overview` |
| Configuration | `GET /ontology`, `POST /ontology/field`, `PUT /ontology/field/{category}/{key}`, `PUT /ontology/sensitivity` |

Authenticated requests carry the session token in `X-User-Token`. If
`CHAT_API_KEY` is set, requests also carry the shared key in `X-API-Key`.

## Roles

| Role | Access |
| --- | --- |
| `admin` | All application areas, account management, schema editing, uploads, and feedback |
| `confidential` | Chat, knowledge, and explore, including confidential values |
| `default` | Chat, with restricted values withheld by the server |

The approved-verifier flag controls the Review area separately. Role and
sensitivity checks happen on the server before data is returned.

For deployment-specific examples, see [Deployment](DEPLOYMENT.md). For common
failures, see [Troubleshooting](troubleshooting.md).
