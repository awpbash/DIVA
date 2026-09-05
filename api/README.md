# `api/`

The FastAPI application serves the web client, chat API, document routes,
review workflow, and administration endpoints. It also owns sessions and the
SQLite application database.

The application-level guide is [Architecture](../docs/architecture.md). The
live API publishes its detailed OpenAPI description at `/docs`.

## Layout

| Path | Responsibility |
| --- | --- |
| `main.py` | FastAPI app, startup checks, health routes, CORS, and SPA serving |
| `settings.py` | Branding and retrieval settings from the environment |
| `deps.py` | Shared Cosmos and model clients |
| `appdb.py` | SQLite accounts, sessions, threads, votes, and feedback |
| `boot_seed.py` | Optional seed of an empty storage volume before startup |
| `review_votes.py` | Review approvals and correction votes |
| `routes/auth.py` | Sign-in, sessions, and accounts |
| `routes/chat.py` | Streaming chat responses |
| `routes/docs.py` | Document catalog and PDF access |
| `routes/evidence.py` | Citation evidence and page rectangles |
| `routes/review.py` | Field review and evidence correction |
| `routes/km.py` | Current values, families, and knowledge exports |
| `routes/graph.py` | Graph overview and subgraph responses |
| `routes/admin.py` | Upload, extraction jobs, configuration, and activity |
| `rag/` | Planner, retrieval tools, answer synthesis, policy, and citation checks |

## Request path

```text
browser → route → session and role check → store or retrieval → response
```

Roles come from the server-side session. The browser does not choose its own
clearance. Retrieval tools filter records before they reach the model or the
client. Chat uses server-sent events so the answer can stream as it is written.

## Local development

From the repository root:

```bash
docker compose up -d cosmos
python -m scripts.setup --check
python -m uvicorn api.main:app --reload --port 8000
```

The Docker Compose development service mounts backend source directories. A
frontend change still requires rebuilding the web bundle or running the Vite
development server from `web/`.

## Authentication

`POST /auth/login` creates a session for an existing account. Clients send the
session in `X-User-Token`. If `CHAT_API_KEY` is set, they also send the shared
key in `X-API-Key`.

The first account comes from `BOOTSTRAP_ADMIN_EMAIL`. The default email-only
sign-in is suitable for local evaluation; a shared deployment should put a real
identity layer in front of the application. See [Deployment](../docs/DEPLOYMENT.md)
and [Security](../SECURITY.md).

## Testing

API tests use temporary SQLite databases, fake model calls, and in-memory
fixtures. They do not need Docker, a model endpoint, or a live Cosmos account.

```bash
python -m pytest tests/api/ tests/rag/ -q
```

When changing a route, update the endpoint table in
[`docs/reference.md`](../docs/reference.md) if the route is public-facing.
