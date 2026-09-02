# Technology Stack and Architecture Reference

Audience: solution architects and technical reviewers. This is a component-level
reference for what the system is built on, how the pieces fit, and what it takes
to run or relocate it. Versions are the minimum pinned floors from
`requirements.txt` and `web/package.json`.

---

## 1. What the system is

A knowledge base and chatbot over scanned contract collections. It reads PDFs,
extracts every value into a fixed operational ontology, anchors each value to its
exact clause and page location, lets reviewers verify fields, and answers
questions with citations that highlight the source clause on the page. It applies
role-based redaction, cross-document aggregation, and supersedence (the current
value after amendments).

**Architecture pattern: agentic GraphRAG.** Retrieval runs as a model-driven
tool-calling loop over two modes: semantic and keyword search for single-fact
lookups, and deterministic structured queries for aggregation, comparison, and
currency. Extraction is schema-first into a defined ontology rather than open text
mining. A human verifies each field before it earns the trusted tier. Provenance
(clause snippet plus page bounding box) is carried end to end, and confidentiality
is enforced on the server, not in the browser.

---

## 2. Stack by layer

### Languages
| Language | Where used |
|---|---|
| Python 3.12 | Backend API, extraction pipeline, operational scripts |
| TypeScript 5.6 | Frontend single-page application |
| Cosmos SQL (NoSQL dialect) | Knowledge-store queries: retrieval, aggregation, supersedence, issued only through `pipeline/store/query.py` |
| SQL (SQLite dialect) | Application metadata store |

### Frontend
| Component | Technology | Role |
|---|---|---|
| UI framework | React 18.3 | Single-page app: chat, review, knowledge, explore, admin |
| Build and dev server | Vite 5.4 with the React plugin | Bundling and hot reload |
| Language and types | TypeScript 5.6 | Type safety across the SPA |
| PDF viewer | react-pdf 9.1 with pdfjs-dist 4.8.69 | Renders source PDFs and draws the bounding-box citation highlights |
| Graph visualization | reactflow 11.11 with elkjs 0.9 | The Explore-graph view: nodes, edges, and automatic layout |
| Answer rendering | react-markdown 9 with remark-gfm 4 | Renders streamed answers with inline citation chips |
| Serving | FastAPI static serving | The built SPA is served by the same FastAPI process as the API, so the browser talks to one origin (no nginx, no CORS) |

### Backend and API
| Component | Technology | Role |
|---|---|---|
| Web framework | FastAPI 0.111 | REST endpoints plus an OpenAPI/Swagger surface, and the static SPA bundle |
| ASGI server | Uvicorn 0.30 (`uvicorn[standard]`) | Runs the API process |
| Streaming | sse-starlette 2.1 | Server-sent events for streamed chat answers |
| Validation | Pydantic 2.7 | Request and response schemas |
| File upload | python-multipart | The admin PDF-upload endpoint |
| App datastore | SQLite via the Python standard library `sqlite3` | Accounts, sessions, chat history, verification votes, usage metering, activity events, feedback, the ontology overlay, and the document registry sidecar |
| Config and runtime | python-dotenv, PyYAML, Jinja2, jsonschema | Declarative config tree, prompt templating, config schema validation |

### Knowledge store and data stores
| Component | Technology | Role |
|---|---|---|
| Knowledge store | Azure Cosmos DB NoSQL | One `kb` container holds everything: record items, edge items, and embeddings, partitioned on `pk`. There is no separate graph or vector database |
| Store access layer | `pipeline/store/` (client, query, model) | The only code that talks to Cosmos. All queries are Cosmos SQL issued through `store.query` |
| Vector search | `COSMOS_VECTOR_MODE=client` or `native` | Exact in-RAM ranking in dev and on the emulator, native DiskANN on real Azure. Verified to return identical top results |
| SDK | azure-cosmos 4.9 or newer, with aiohttp for the async client | Application-to-store connectivity |
| Local emulation | Cosmos DB emulator (Linux vnext) in Docker | Offline development. Emulator data is disposable: `python -m scripts.rebuild_kb` repopulates the container from `storage/` for free |
| App metadata | SQLite (single file, `storage/app.db`) | The only relational state in the system |
| File store seam | `pipeline/filestore.py` with azure-storage-blob | Local `storage/` folder in dev, a volume on the demo host, Azure Files or an optional Blob mirror on Azure |

### AI and machine learning
| Component | Technology | Role |
|---|---|---|
| Model client | OpenAI SDK 1.40 | Runs against OpenAI directly, or Azure AI Foundry, or any OpenAI-compatible endpoint by setting a base URL. No code change to switch |
| Models (defaults, configurable) | gpt-5.4 for reasoning, gpt-5.4-mini for vision and general text, text-embedding-3-large for embeddings | Planning, page understanding, extraction, answer synthesis, and question/document embeddings. The embedding cache is keyed to the embedding model |
| Document reader, local mode | RapidOCR 3.0 with ONNX Runtime 1.26 (`READER=rapidocr`, the dev default) | On-box OCR using PaddleOCR-derived ONNX models, followed by a vision-model cleanup pass. No cloud OCR dependency |
| Document reader, cloud mode | Azure AI Content Understanding (`READER=cu`, GA API 2025-11-01) | One paid analyze call per document (`pipeline/extraction/cu_read.py`, `CU_ENDPOINT` and `CU_KEY`). Both readers produce the same page-markdown shape, so the downstream pipeline is identical |
| Resilience | tenacity | Retry and backoff on model calls |

### Document processing
| Technology | Role |
|---|---|
| pypdfium2 | PDF to page-image rendering and page geometry. Chosen over PyMuPDF, which is AGPL and cannot ship inside an MIT project |
| Pillow | Page-image dimensions used by extraction stages |
| openpyxl | Writes the Knowledge tab's Excel export |

### Infrastructure and deployment
| Component | Technology |
|---|---|
| Containerization | Docker and Docker Compose. Two services: app (FastAPI serving the API and the SPA) and cosmos (the emulator, skipped when pointing at live Azure) |
| Knowledge-store packaging | None needed. The container is a disposable projection: `rebuild_kb` drops and repopulates it from `storage/` with no LLM cost |
| Target platforms | Local Docker as the primary target, plus any host that runs a container: a small cloud box, a platform-as-a-service, or an in-tenant Azure deployment. One image runs everywhere, every difference is an environment variable |

### Testing and evaluation
| Technology | Role |
|---|---|
| pytest | Around 900 offline unit tests with no network, database, or model calls |
| Custom deterministic scorer | Per-field extraction grading against a human gold set you create for your domain |

---

## 3. Runtime topology

Two containers behind one browser origin:

```
browser
   |
   v
app  (FastAPI + Uvicorn, port 8000)   serves the SPA and the JSON API
   |            |
   |            +--> model endpoint (OpenAI direct or Azure AI Foundry)   query embeddings, synthesis, extraction
   v
cosmos  (one kb container: records, edges, vectors. Emulator in dev, live account in prod)
   ^
   |
storage volume (app.db SQLite, source PDFs, page images, extraction artifacts, embedding cache)
```

Two places hold state: Cosmos (the projected knowledge base) and the storage
volume (the SQLite application file plus the pipeline artifacts). The Cosmos
container itself is rebuildable from storage at any time, so the storage volume
is the real system of record.

---

## 4. The mode switches

One codebase, one image. Each deployment difference is a single environment
variable, so moving between a laptop, the demo, and the corporate tenant is
configuration, not code.

| Concern | Switch | Dev default | Production option |
|---|---|---|---|
| Document reader | `READER` | `rapidocr` (free, on-box OCR plus vision cleanup) | `cu` (Azure AI Content Understanding, `CU_ENDPOINT` and `CU_KEY`) |
| Vector search | `COSMOS_VECTOR_MODE` | `client` (exact, in-RAM) | `native` (Cosmos DiskANN) |
| LLM and embeddings | `OPENAI_BASE_URL`, `OPENAI_EMBED_BASE_URL` | OpenAI direct | Azure AI Foundry (resource root plus `/openai/v1`) |
| Database target | `COSMOS_URI`, `COSMOS_KEY`, `COSMOS_CONTAINER` | Emulator in Docker, container `kb_dev` for dev work | Live Azure account, container `kb` reserved for the demo |
| Files | Blob and Files env vars (`pipeline/filestore.py`) | Local `storage/` | A mounted volume, Azure Files, or an optional Blob mirror |

Both readers emit the same page-markdown shape, and both vector modes return the
same top results, so a mode flip never changes downstream behavior.

---

## 5. Data flow, end to end

1. **Ingest.** A PDF is rendered to page images and geometry (pypdfium2), then read
   by the selected reader: on-box OCR plus a vision cleanup pass, or one Azure
   Content Understanding call.
2. **Extract.** A model fills the fixed ontology fields directly from the document
   (schema-first), anchoring each value to a verbatim snippet and a bounding box.
   "Not Stated" is a valid value.
3. **Align (knowledge build).** The knowledge base is projected into the Cosmos
   container: ontology-field records, canonical party hubs, a document family DAG
   declared in the recitals (amends, supersedes, novates), and per-field
   supersedence walked in pure code.
4. **Verify.** Reviewers vote on each field (approve, reject, or amend) with a
   GitHub-style consensus. A verified value earns the human-validated tier and
   propagates to the knowledge base immediately.
5. **Retrieve and answer.** A question is planned, scoped to the relevant
   documents, answered by an agentic loop over semantic search, keyword search,
   and deterministic aggregation, then synthesized with inline citations that
   highlight the clause on the page.

---

## 6. Security and access posture

- **Role-based access control** is server-derived. The client never selects its own
  clearance. Roles are admin, confidential, and default, with a separate verifier
  flag that opens the review workflow.
- **Redaction is enforced at the source.** Confidential values are filtered before
  the model sees them and again on the citations returned to the browser, so a
  restricted value never leaves the server.
- **Confidentiality follows the content, not the query path.** Sensitivity is a
  property of fields and text, honored identically by the structured lookups and
  the text search.
- **Document reading can stay inside the boundary.** The default reader runs OCR
  on-box with no external call. The cloud reader is Azure AI Content
  Understanding, so even the paid path stays inside the Azure tenant.
- **Authentication is demo-grade today** (passwordless account selection). It is a
  contained swap to a real identity provider. This is the main item to close before
  external production exposure.

---

## 7. Portability notes for relocation

- **Model layer is endpoint-agnostic.** Point `OPENAI_BASE_URL` and the key at a
  corporate Azure AI Foundry deployment and the system uses it with no code
  change. Chat and embeddings can target different endpoints if needed.
- **One store, no separate vector database.** Records, edges, and embeddings live
  in a single Cosmos container, which keeps the data plane to one managed service
  plus a small SQLite file.
- **State is small and movable.** The system of record is the `storage/` folder
  plus the SQLite file. The Cosmos container is reseeded from them by
  `rebuild_kb` with no LLM cost, embeddings included via the local cache.
- **Batch versus serving are separable.** Ingestion and extraction are heavy and can
  run offline or as a batch job. The serving path only needs the projected
  container, the document artifacts, and a working model endpoint.

---

## 8. Version reference

| Area | Package | Floor |
|---|---|---|
| Backend | fastapi | 0.111 |
| Backend | uvicorn[standard] | 0.30 |
| Backend | pydantic | 2.7 |
| Backend | sse-starlette | 2.1 |
| Backend | openai | 1.40 |
| Backend | azure-cosmos | 4.9 |
| Backend | azure-storage-blob | 12.20 |
| Backend | rapidocr | 3.0 |
| Backend | onnxruntime | 1.26 |
| Backend | pypdfium2 | 4.30 |
| Backend | openpyxl | 3.1 |
| Frontend | react | 18.3 |
| Frontend | typescript | 5.6 |
| Frontend | vite | 5.4 |
| Frontend | pdfjs-dist | 4.8.69 |
| Frontend | react-pdf | 9.1 |
| Frontend | reactflow | 11.11 |
| Data plane | Azure Cosmos DB NoSQL | emulator vnext-preview in dev, live account in prod |
| Runtime | Python | 3.12 |

For exact resolved versions in a given build, read `requirements.txt`,
`web/package-lock.json`, and the base image tags in the Dockerfile.
