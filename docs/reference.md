# Reference

Settings, commands and endpoints. Look things up here.

## 01 · Settings

Every environment difference is an environment variable, so the same image
runs on a laptop, a small cloud box and a private tenant.
[`.env.example`](../.env.example) is the annotated source and explains each
setting where it sits. This table is the summary.

### Required

| Variable | Notes |
| --- | --- |
| `OPENAI_API_KEY` | The only setting with no working default |

### Models

| Variable | Default | Notes |
| --- | --- | --- |
| `OPENAI_BASE_URL` | OpenAI | Point at any OpenAI-compatible endpoint, including one inside your own network. Match the model names to what it serves |
| `OPENAI_TEXT_MODEL` | `gpt-5.4-mini` | The everyday model |
| `OPENAI_VISION_MODEL` | `gpt-5.4-mini` | Page reading and correction |
| `OPENAI_REASONING_MODEL` | `gpt-5.4` | Harder extraction passes |
| `OPENAI_EMBED_MODEL` | `text-embedding-3-large` | Vector search |
| `OPENAI_EMBED_BASE_URL`, `OPENAI_EMBED_API_KEY` | Falls back to the main endpoint | For when embeddings live behind a different service |

**Your documents go to whichever endpoint you configure.** If they cannot
leave your network, host the endpoint yourself. Nothing else phones home.

### Domain and identity

| Variable | Default | Notes |
| --- | --- | --- |
| `VERBATIM_DOMAIN` | Autodetected | Which document schema this instance serves. With more than one pack present and no declaration, the app refuses to start |
| `APP_NAME` | `Verbatim` | Served to the browser by `GET /branding`, so a rename is a restart |
| `APP_TAGLINE` | See `.env.example` | Shown under the name |
| `APP_DOCUMENT_NOUN` | `document` | What the interface calls one of the things this instance holds. Set it to `record`, `application`, `case` or whatever your users say |
| `APP_DOCUMENT_NOUN_PLURAL` | `documents` | The plural of the above |
| `CHAT_CORS_ORIGIN_REGEX` | unset | Regex of allowed browser origins. Only needed when the frontend is served from a different host than the API |
| `STORAGE_SEED_URL` | unset | A tarball fetched at boot to populate an empty `storage/`. How a fresh container gets a corpus |
| `STORAGE_SEED_FORCE` | `false` | Re-apply the seed even when `storage/` already has content |
| `CHAT_EMAIL` / `CHAT_TOKEN` | unset | Credentials `scripts/ask.py` and `scripts/_session.py` use to talk to a running instance |
| `OPENAI_NORMALISE_MODEL` | See `.env.example` | Model for the normalisation pass |
| `OPENAI_NORMALISE_HEAVY_MODEL` | See `.env.example` | Heavier model for value-bearing categories |
| `BOOTSTRAP_ADMIN_EMAIL` | `admin@localhost` | The account created on first boot when there are none. Sign-in is passwordless, so this address is the key to the instance |
| `BOOTSTRAP_ADMIN_NAME` | `Administrator` | |

### Knowledge store

| Variable | Default | Notes |
| --- | --- | --- |
| `COSMOS_URI` | `http://localhost:8081` | The emulator that `docker compose up -d cosmos` starts |
| `COSMOS_KEY` | The emulator's published dev key | Set this on a real account |
| `COSMOS_DB` | `verbatim` | |
| `COSMOS_CONTAINER` | `kb` | One container holds records, edges and vectors |
| `COSMOS_VECTOR_MODE` | `client` | `client` is exact and in memory, right for development. `native` uses the database index, for a real account |

### Reader

| Variable | Default | Notes |
| --- | --- | --- |
| `READER` | `rapidocr` | Local OCR plus a model correction pass. Needs no cloud service |
| | `cu` | Azure Content Understanding. One paid call per document, cached |
| `CU_ENDPOINT`, `CU_KEY`, `CU_ANALYZER` | | Only for `READER=cu` |
| `RENDER_DPI` | `300` | Page rasterisation. Lower is cheaper and less accurate |
| `VISION_PAGE_CONCURRENCY` | `4` | How many pages are read at once |

### Storage

| Variable | Default | Notes |
| --- | --- | --- |
| `STORAGE_ROOT` | `./storage` | Back this up. Everything else is rebuildable |
| `AZURE_STORAGE_CONNECTION_STRING`, `AZURE_BLOB_ACCOUNT_URL`, `AZURE_BLOB_CONTAINER` | | Mirror uploads to Blob when the container has no persistent disk |

### Review

| Variable | Default | Notes |
| --- | --- | --- |
| `REVIEW_MIN_APPROVALS` | `1` | How many verifiers must approve a value before the field counts as human verified. Raise it to require two people |
| `REVIEW_CORRECTION_APPROVALS` | `2` | How many verifiers a *corrected* value needs. A correction overrides the machine everywhere, so the default asks for a second pair of eyes. **Set this to `1` on a single-operator instance**, or nobody there can ever finish a correction |

### API

| Variable | Default | Notes |
| --- | --- | --- |
| `CHAT_API_KEY` | Unset | When set, every route requires the same value in an `X-API-Key` header. It keeps an internal deployment off the open internet. It is not per-user authentication and does not replace it |
| `CHAT_CORS_ORIGINS` | Unset | Comma-separated allowed origins |
| `CHAT_DISABLED_TOOLS` | Unset | Comma-separated tool names to withhold from the agent |
| `LOG_FORMAT` | Human readable | Set to `json` in production |

<a id="commands"></a>

## 02 · Commands

Run everything as a module: `python -m scripts.setup`, never
`python scripts/setup.py`. On Windows set `PYTHONUTF8=1` first.

### Setup and accounts

| Command | Cost | What |
| --- | --- | --- |
| `python -m scripts.setup` | Free | Clone to working instance. Creates `.env`, resolves the domain, compiles the configuration, creates the database and the first admin |
| `python -m scripts.setup --check` | Free | Report only, change nothing |
| `python -m scripts.setup --check-models` | A fraction of a cent | Also send one embedding request to prove the endpoint answers |
| `python -m scripts.accounts list` | Free | Every account and its role |
| `python -m scripts.accounts add <email> --role admin` | Free | Add an account |
| `python -m scripts.accounts role <email> <role>` | Free | Change an authority level |
| `python -m scripts.accounts verifier <email> --on` | Free | Grant the approved-verifier flag |
| `python -m scripts.accounts remove <email>` | Free | Remove an account. Keeps its events and votes as history |

### Documents and the knowledge base

| Command | Cost | What |
| --- | --- | --- |
| `python -m pipeline.ingest <pdf>` | **Paid on first run** | Read one document: pages, text, geometry, embeddings. Cached, so a second run does nothing. This is the reading half only |
| `python -m pipeline.kb.field_llm --all` | **Paid on first run** | Fill the field schema from every document that has been read. Add `--force` after a schema change, because the cache is keyed by document alone |
| `python -m scripts.rebuild_kb` | Free | Drop and rebuild the whole graph from local files. No model calls |
| `python -m scripts.build_km` | Free | Rebuild the aligned knowledge layer. Also folded into `rebuild_kb` |
| `python -m scripts.delete_doc <doc_id>` | Free | Remove one document from everywhere |
| `python -m scripts.reconcile_kb` | Free | Read-only cross-layer audit |
| `python -m scripts.view_coverage` | Free | What the live graph holds, per capture mechanism |
| `python -m scripts.render_ontology` | Free | Draw the declared ontology as Mermaid diagrams |
| `python -m scripts.reanchor_evidence` | Free | Re-anchor stored evidence rectangles from their cited blocks |
| `python -m scripts.graph_grep <doc_id> <text>` | Free | Search the store's text for one document. A debugging helper |

### Checking your work

| Command | Cost | What |
| --- | --- | --- |
| `python -m pytest tests/ -q` | Free | The deterministic suite. No model calls, no live database |
| `python -m ruff check .` | Free | Lint, scoped to correctness rules |
| `cd web && npm run build` | Free | The frontend build |
| `python -m eval.extraction.score --run NAME` | Free | Grade extraction against your human-verified gold set |
| `python -m eval.extraction.score --dump` | Free | Per-document field extractions, for seeding a private gold set |
| `python -m scripts.ask "<question>"` | **Paid** | One question at the live chat endpoint. `--role`, `--doc`, `--retrieval-only` |

## 03 · HTTP API

The application serves its own interactive documentation at `/docs`, generated
from the code, which is the authoritative version. This is the map.

Authentication is a session token in an `X-User-Token` header, minted by
`POST /auth/login`. Roles are resolved server-side from the session. Clients
never send their own clearance.

### Public

| | |
| --- | --- |
| `GET /healthz` | Liveness |
| `GET /readyz` | Readiness, including the database and storage |
| `GET /branding` | Instance name, tagline, active domain, version |
| `POST /auth/login` | Sign in by email. Unknown addresses are rejected |
| `POST /auth/logout` | End the session |

### Session

| | |
| --- | --- |
| `GET /auth/me` | The current user and which tabs they may use |
| `POST /chat` | Ask a question. Server-sent events, streaming the answer with `[ev:id]` citations |
| `GET /threads`, `PUT /threads` | The caller's own chat history |
| `GET /documents` | The catalog, with review tier and family metadata |
| `GET /pdf/{doc_id}` | The original PDF |
| `GET /evidence/{evidence_id}` | Page number, rectangles and snippet for one citation |
| `POST /feedback`, `GET /feedback/mine` | Report an answer |

### Cleared eyes: confidential and admin

| | |
| --- | --- |
| `GET /km/families`, `GET /km/family/{group}` | Document families and their chains |
| `GET /km/aggregate` | Deterministic aggregation over verified fields |
| `GET /km/export` | The aligned knowledge layer, exported |
| `GET /km/page/{doc_id}/{page_no}` | One page with its field anchors |
| `GET /graph/subgraph`, `GET /graph/overview`, `GET /graph/hubs` | The knowledge graph |

### Approved verifiers

| | |
| --- | --- |
| `GET /review/docs`, `GET /review/heatmap` | What needs review, triaged |
| `GET /review/doc/{doc_id}` | Every field with its evidence |
| `GET /review/page/{doc_id}/{page_no}`, `GET /review/blocks/{doc_id}/{page_no}` | Page image and text blocks |
| `POST /review/doc/{doc_id}/field/{field_key}/verify` | Approve, reject or amend. A vote, resolved by majority |
| `POST /review/doc/{doc_id}/field/{field_key}/attach-evidence` | Fix a wrong anchor |

### Admin

| | |
| --- | --- |
| `POST /admin/upload` | Upload a PDF. Extraction starts on upload |
| `POST /admin/extract/{doc_id}` | Re-run extraction |
| `GET /admin/jobs` | Background job state |
| `GET /admin/overview`, `GET /admin/metrics`, `GET /admin/activity` | Dashboard, usage, changelog |
| `GET /auth/accounts`, `POST /auth/accounts`, `PUT /auth/accounts/{email}` | Account management |
| `GET /ontology`, `POST /ontology/field`, `PUT /ontology/field/{category}/{key}`, `DELETE /ontology/field/{category}/{key}` | Live schema editing |
| `PUT /ontology/sensitivity`, `GET /policy`, `PUT /policy` | Sensitivity classification |
| `GET /registry/folders`, `POST /registry/folders`, `PATCH /registry/folders/{id}` | Document families as named folders |
| `GET /feedback/admin`, `PUT /feedback/admin/{id}` | The feedback inbox |

## 04 · Access levels

| Role | Reaches |
| --- | --- |
| `admin` | Everything, plus review, accounts, schema editing, upload and the feedback inbox |
| `confidential` | Chat, knowledge and explore, including values classified confidential |
| `default` | Chat only, with confidential values withheld before they leave the server |

The approved-verifier flag opens the review workspace independently of role,
because verification is a vote and needs more than one person, but only vetted
people. What a verifier can see there is still clearance-filtered on the
server.
