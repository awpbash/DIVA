# Deployment

The production image contains the API, web client, pipeline, scripts, and
documentation. It expects a persistent directory mounted at `/app/storage` and
an environment file with the model and database settings.

## Before deploying

Prepare:

- a host that can run Docker Compose
- a persistent volume for `storage/`
- an OpenAI-compatible model endpoint, or Azure AI Foundry
- an Azure Cosmos DB account for a shared deployment
- a reverse proxy or identity layer in front of the application

The local Cosmos emulator is useful for development. It is not a durable
production database. Set `COSMOS_URI` and `COSMOS_KEY` to a real account for a
shared deployment.

## Start the production stack

```bash
git clone https://github.com/awpbash/diva.git
cd diva
cp .env.example .env
```

Set at least `OPENAI_API_KEY`, `COSMOS_URI`, `COSMOS_KEY`, and
`BOOTSTRAP_ADMIN_EMAIL` in `.env`. Then run:

```bash
python -m scripts.setup --check
docker compose -f docker-compose.prod.yml up -d --build
```

The production Compose file:

- builds one immutable application image
- mounts only `./storage` into the app
- binds port 8000 to loopback
- disables the development reload process
- writes JSON logs with rotation

Check the service:

```bash
curl -s http://localhost:8000/healthz
curl -s http://localhost:8000/readyz
docker compose -f docker-compose.prod.yml ps
```

`/healthz` reports process liveness. `/readyz` checks the configured
dependencies and is the better endpoint for a load balancer.

## Put authentication in front

The built-in local sign-in uses an email address. For a shared instance, put a
reverse proxy or identity-aware gateway in front, terminate TLS there, and
forward all paths to the application. The application still applies its own
roles and sensitivity rules after the request is authenticated.

Example Nginx settings:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

`proxy_buffering off` keeps streamed chat responses visible as they arrive.
`proxy_read_timeout` gives longer extraction and chat requests time to finish.

If a second network gate is useful, set `CHAT_API_KEY`. Clients must then send
the same value in `X-API-Key`. This is a shared gate, not per-user identity.

## Create accounts

The account named by `BOOTSTRAP_ADMIN_EMAIL` is created on the first boot when
the application database has no accounts. Add the remaining accounts from the
admin screen or the command line:

```bash
docker compose -f docker-compose.prod.yml exec app \
  python -m scripts.accounts add person@example.com --role confidential
docker compose -f docker-compose.prod.yml exec app \
  python -m scripts.accounts verifier person@example.com --on
```

| Role | Access |
| --- | --- |
| `admin` | All areas, account management, uploads, schema editing, and feedback |
| `confidential` | Chat, knowledge, and explore, including confidential values |
| `default` | Chat, with restricted values withheld by the server |

Review access is granted by the verifier flag. Set review approval counts in
`.env` when more than one person will review corrections.

## Back up the right files

Back up the whole `storage/` directory, especially:

| Path | Contains |
| --- | --- |
| `storage/raw/` | Original PDFs |
| `storage/app.db` | Accounts, sessions, threads, review votes, and feedback |
| Remaining `storage/` | Rendered pages, extraction artifacts, geometry, and embedding cache |

The Cosmos container is rebuildable from these files:

```bash
docker compose -f docker-compose.prod.yml exec app \
  python -m scripts.rebuild_kb
```

Use a scheduled file backup or volume snapshot. Keep the PDFs and
`storage/app.db`. Losing either requires recovery from a separate source.

## Updates and schema changes

```bash
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

After changing the domain schema, ontology, or field rules, re-run extraction
for affected documents and rebuild the knowledge projection:

```bash
docker compose -f docker-compose.prod.yml exec app \
  python -m pipeline.kb.field_llm --all --force
docker compose -f docker-compose.prod.yml exec app \
  python -m scripts.rebuild_kb
```

Read [CHANGELOG.md](../CHANGELOG.md) before a version update. Keep the image
and the mounted storage from the same deployment when diagnosing a problem.

## Storage without a permanent disk

If the host has no durable local disk, set `STORAGE_SEED_URL` to a tarball that
contains a `storage/` directory. DIVA applies it only to an empty volume. For
ongoing persistence, configure the optional Azure Blob mirror with
`AZURE_BLOB_ACCOUNT_URL` or `AZURE_STORAGE_CONNECTION_STRING`.

## Sizing and reader choice

The workload is quiet while users read and bursty during ingestion. Start with
2 vCPUs and 2 GB of memory for a small instance, then adjust from observed
processing time and model rate limits.

`VISION_PAGE_CONCURRENCY` controls parallel page work. `RENDER_DPI` controls
page image size, and 300 is the default. The local `rapidocr` reader uses CPU on the
application host. The `cu` reader shifts page reading to one Azure analysis
call per document.

## Common operational checks

```bash
docker compose -f docker-compose.prod.yml logs -f app
docker compose -f docker-compose.prod.yml exec app \
  python -m scripts.setup --check
docker compose -f docker-compose.prod.yml exec app \
  python -m scripts.reconcile_kb
docker compose -f docker-compose.prod.yml exec app \
  python -m scripts.accounts list
```

For symptoms and fixes, see [Troubleshooting](troubleshooting.md). For the
complete environment list, see [Reference](reference.md).
