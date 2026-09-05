# Technology reference

This page lists the main technologies and the switches that change runtime
behavior. It is a reference, not a second architecture guide; see
[Architecture](architecture.md) for component relationships.

## Main stack

| Area | Technology | Role |
| --- | --- | --- |
| Backend | Python 3.12, FastAPI, Uvicorn | API, background jobs, and static-file serving |
| Configuration | Pydantic, PyYAML, JSON Schema, Jinja2 | Environment and domain configuration, prompt rendering, and validation |
| Frontend | React, TypeScript, Vite | Single-page workspace |
| PDF rendering | pypdfium2 and Pillow | Render pages and read image dimensions |
| Knowledge store | Azure Cosmos DB NoSQL | Records, edges, and embeddings in one container |
| Application state | Python `sqlite3` | Accounts, sessions, threads, votes, and feedback |
| Chat streaming | Server-sent events via `sse-starlette` | Stream answers to the browser |
| PDF viewer | `react-pdf` and `pdfjs-dist` | Show the source page and citation rectangles |
| Graph view | `reactflow` and `elkjs` | Layout and display related records |
| Spreadsheet export | `openpyxl` | Export knowledge results from the UI |

## Document readers and OCR geometry

| Mode | Package or service | Geometry source |
| --- | --- | --- |
| Local, `READER=rapidocr` | `rapidocr` with `onnxruntime` | RapidOCR line boxes, normalised by the adapter |
| Cloud, `READER=cu` | Azure Content Understanding | Layout and table polygons from the analysis response |

The Docker image installs `rapidocr>=3.0` and `onnxruntime>=1.26`. It does not
install `paddleocr`. RapidOCR uses models derived from the PP-OCR family, which
is why some implementation comments call the models “PaddleOCR-derived.” That
does not mean the PaddleOCR package is part of the image.

For the local reader, the model sees the page and the OCR lines but does not
write citation coordinates. DIVA keeps the OCR line boxes and builds block and
row rectangles from them. The geometry then travels through the merge and
evidence records to the PDF viewer.

## Model providers

The application uses the OpenAI Python client. Set `OPENAI_BASE_URL` to an
OpenAI-compatible endpoint when the models are hosted by Azure AI Foundry, a
gateway, or a private service. Chat and embeddings can use separate endpoints
through `OPENAI_EMBED_BASE_URL` and `OPENAI_EMBED_API_KEY`.

Default model names are configurable and are read from the environment:

| Variable | Default | Used for |
| --- | --- | --- |
| `OPENAI_VISION_MODEL` | `gpt-5.4-mini` | Page correction and table repair |
| `OPENAI_TEXT_MODEL` | `gpt-5.4-mini` | General text tasks and chat |
| `OPENAI_REASONING_MODEL` | `gpt-5.4` | Whole-document extraction passes |
| `OPENAI_NORMALISE_MODEL` | Text model | Field normalisation |
| `OPENAI_NORMALISE_HEAVY_MODEL` | `gpt-5.4` | Value-bearing categories |
| `OPENAI_EMBED_MODEL` | `text-embedding-3-large` | Semantic search |

The model endpoint receives the documents sent to its reader or extraction
call. Choose an endpoint that matches the data-handling requirements of the
deployment.

## Storage and search

Cosmos DB stores graph records, relationships, and vectors in one container.
`pipeline/store/` is the access boundary. Local development uses the emulator
and exact in-memory vector ranking; a real Azure account can use Cosmos native
vector search.

| Setting | Development default | Other option |
| --- | --- | --- |
| `COSMOS_URI` | `http://localhost:8081` | A live Cosmos endpoint |
| `COSMOS_CONTAINER` | `kb` | A deployment-specific container |
| `COSMOS_VECTOR_MODE` | `client` | `native` for the Cosmos vector index |
| `STORAGE_ROOT` | `./storage` | A mounted persistent directory |

Cosmos is a searchable projection. The original files, extraction artifacts,
and application state are under `storage/`; use
`python -m scripts.rebuild_kb` to recreate the projection.

## Runtime switches

| Concern | Setting | Effect |
| --- | --- | --- |
| Active domain | `VERBATIM_DOMAIN` | Selects the analyzer, pack, ontology, and field view |
| Reader | `READER` | Chooses `rapidocr` or `cu` |
| Page size | `RENDER_DPI` | Controls rendered image resolution; default `300` |
| Reader concurrency | `VISION_PAGE_CONCURRENCY` | Limits pages processed in parallel; default `4` |
| Review approvals | `REVIEW_MIN_APPROVALS`, `REVIEW_CORRECTION_APPROVALS` | Sets the approval count for normal and corrected values |
| API protection | `CHAT_API_KEY`, CORS settings | Adds shared-key and browser-origin controls |
| Logs | `LOG_FORMAT` | Use `json` for structured production logs |

For the complete environment list, use [Reference](reference.md) and the
annotated [`.env.example`](../.env.example).

## Testing

The backend test suite uses fakes and fixtures, so it does not need a model
endpoint, live database, or network connection. Frontend tests run with
Vitest. The relevant commands are in [Contributing](../CONTRIBUTING.md) and
[tests/README.md](../tests/README.md).

## Dependency files

- `requirements.txt` contains the Python runtime and test dependencies.
- `requirements-dev.txt` contains development-only checks.
- `web/package.json` and `web/pnpm-lock.yaml` contain the frontend dependencies.
- `Dockerfile` builds the web bundle and the Python application image.
