# Deploying

This is one container and one database. If you can run Docker on a small server,
you can run this. What follows is the whole path: what to decide first, how to
put it up, what to protect it with, and what to back up.

Read [SECURITY.md](../SECURITY.md) first. The short version is repeated below
because it changes what a safe deployment looks like.

---

## Decide these four things first

**1. Where the model calls go.** Extraction and chat send document text and page
images to whatever endpoint you configure in `OPENAI_BASE_URL`. If your documents
cannot go to a third party, point it at an endpoint you control. Nothing else
leaves the machine.

**2. Where the knowledge store lives.** The local emulator that ships in the
compose file is a development tool: not durable, not backed up, not supported for
production. For anything that matters, create a real Azure Cosmos DB account
(NoSQL API), set `COSMOS_URI` and `COSMOS_KEY`, and set
`COSMOS_VECTOR_MODE=native` so search uses the database's own index instead of
ranking in memory.

The store is a **projection**, not the source of truth. If you lose it,
`python -m scripts.rebuild_kb` rebuilds it from your files, for free, with no
model calls. That is worth knowing before you decide how much to spend
protecting it.

**3. Where the files live.** `storage/` holds every uploaded PDF, every page
image, and every extraction artifact. **This is the one thing to back up.**
Attach a persistent volume at `/app/storage`. If your host has no persistent
disk, set the Azure Blob variables and the app mirrors the directory to blob
storage and restores it at boot.

**4. What sits in front.** See the next section. This is the decision people skip.

---

## The posture problem, stated plainly

**Sign-in is passwordless.** Typing an email address that has an account signs
you in as that account. No password, no second factor, no identity provider.

That is a sensible default for evaluating the software on your own machine. It
is not authentication, and an instance reachable from the internet is open to
anyone who can guess an email address. The app says so in its own log on every
boot.

You have three reasonable options:

| Option | What it means | When |
| --- | --- | --- |
| **Private network only** | The app is reachable from your office network or a VPN and nowhere else | Simplest, and enough for most internal use |
| **Reverse proxy with real auth** | A proxy in front terminates TLS and authenticates before forwarding | The normal production answer |
| **`CHAT_API_KEY`** | Every request must carry a shared secret | A blunt lock. It keeps strangers out. It does not tell users apart, so it is not a substitute for the two above |

What does *not* need replacing is the access control **behind** the login. Roles
and per-field sensitivity are enforced on the server before data leaves the API,
so a user without clearance never receives the confidential value. Swapping the
login does not disturb any of it: `api/routes/auth.py` mints the session and
`api/appdb.py` stores accounts, and the rules live elsewhere.

---

## Putting it up

On a server with Docker installed:

```bash
git clone <this repo> && cd verbatim
cp .env.example .env
```

Edit `.env`. At minimum set `OPENAI_API_KEY`, the Cosmos variables for your real
account, and `BOOTSTRAP_ADMIN_EMAIL` to your own address. Then:

```bash
pip install -r requirements.txt
python -m scripts.setup --check      # confirms the configuration before you build

docker compose -f docker-compose.prod.yml up -d --build
```

The production compose file is standalone rather than an overlay, because
Compose merges list fields by appending and an overlay therefore cannot remove
the development source mounts. It publishes the port on loopback only, runs
without the reload watcher, mounts nothing but `storage/`, and rotates its logs.

Check it came up:

```bash
curl -s localhost:8000/healthz     # the process is alive
curl -s localhost:8000/readyz      # its dependencies answer, with a breakdown
docker compose -f docker-compose.prod.yml logs -f app
```

`/healthz` is liveness only, on purpose. An orchestrator must not kill the
container because the database blinked. `/readyz` is what a load balancer should
watch.

### A reverse proxy

Any proxy works. The app serves the API and the web bundle from one origin, so
there is no route list to maintain and no CORS to configure. Forward everything:

```nginx
server {
    listen 443 ssl;
    server_name verbatim.example.com;

    # your TLS configuration, and your authentication, here

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Answers stream token by token over server-sent events. Without this,
        # the proxy holds the whole answer and releases it at the end, which
        # looks like the app hanging.
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

The two settings at the bottom matter more than they look. Chat answers arrive as
a stream, and a buffering proxy turns a responsive app into one that appears
frozen for thirty seconds.

---

## First run

Sign in as the address in `BOOTSTRAP_ADMIN_EMAIL`. That account is created on the
very first boot, only when there are no accounts at all, so it cannot overwrite
anything later.

From the admin area, add the accounts that need access and set their roles. Or
from the command line:

```bash
docker compose -f docker-compose.prod.yml exec app \
  python -m scripts.accounts add someone@example.com --name "Their Name" --role confidential
```

| Role | Reaches |
| --- | --- |
| `admin` | Everything, plus review, accounts, schema editing, upload, feedback |
| `confidential` | Chat, knowledge, explore, including values tagged confidential |
| `default` | Chat only, with confidential values withheld server-side |

The separate verifier flag opens the review workspace regardless of role.

Then upload a PDF. Extraction starts on upload, the document becomes searchable
when it finishes, and verification happens afterwards without blocking anyone.

---

## Backups

Back up **`storage/`**. Everything else is either rebuildable or configuration.

| What | Why | If you lose it |
| --- | --- | --- |
| `storage/raw/` | The original PDFs | Gone. Re-upload from wherever they came from. |
| `storage/app.db` | Accounts, sessions, chat history, verification votes, the schema overlay | **The expensive loss.** Every human verification decision lives here. |
| `storage/` (the rest) | Page images, extraction artifacts, the embedding cache | Rebuildable, but only by paying for extraction again |
| The knowledge store | The searchable projection | Free to rebuild: `python -m scripts.rebuild_kb` |

A nightly copy of the whole directory is enough. It is ordinary files and one
SQLite database.

```bash
docker compose -f docker-compose.prod.yml stop app
tar czf "verbatim-$(date +%F).tgz" storage/
docker compose -f docker-compose.prod.yml start app
```

Stopping the app first is not strictly required, but it removes any question
about a half-written SQLite file.

---

## Updating

```bash
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

The image rebuild is the update. Nothing migrates by hand: the app-state database
adds columns it needs at boot, and the knowledge store is rebuildable.

**After changing anything about the schema or the ontology, rebuild the knowledge
layer**, which is free:

```bash
docker compose -f docker-compose.prod.yml exec app python -m scripts.rebuild_kb
```

Read [CHANGELOG.md](../CHANGELOG.md) before a major version step.

---

## Operating it

```bash
# Free, read-only, and safe to run any time
docker compose -f docker-compose.prod.yml exec app python -m scripts.setup --check
docker compose -f docker-compose.prod.yml exec app python -m scripts.reconcile_kb
docker compose -f docker-compose.prod.yml exec app python -m scripts.accounts list

# Free, rewrites the searchable projection from your files
docker compose -f docker-compose.prod.yml exec app python -m scripts.rebuild_kb
```

`LOG_FORMAT=json`, which the production compose file sets, emits one JSON object
per line. Every request is logged with its path, status and duration, and the
retrieval agent logs the tools it chose, which is usually what you want when an
answer looks wrong.

### What costs money

Two things, both cached, so you pay once:

- **Ingesting a document**, roughly a dollar on the reference corpus, dominated
  by reading the pages. Re-running is free.
- **Asking a question**, small change each.

Everything else, rebuilding, scoring, reconciling, the tests, is free and calls
no model. Per-account token usage is in the admin area.

---

## Sizing

One small server is enough for a team. The reference deployment runs comfortably
in 2 GB of memory with 2 vCPUs, and the load is bursty: idle between questions,
busy during extraction.

Two knobs matter:

- `VISION_PAGE_CONCURRENCY` (default 4) is how many pages are read in parallel.
  Raise it to ingest faster, lower it if you hit rate limits.
- `RENDER_DPI` (default 300) is the page image resolution. Lower is cheaper and
  faster. Do not go below 200 or OCR accuracy on small print falls away, and the
  evidence highlights get coarser with it.

If you run local OCR (`READER=rapidocr`, the default), extraction is CPU-bound
and wants more cores. The cloud reader (`READER=cu`) trades that for one paid
call per document.

---

## Deploying somewhere other than a server

The image is ordinary and has no host-specific code. It needs an environment
file, a persistent volume at `/app/storage`, and a reachable Cosmos endpoint.

If your host has **no** persistent disk, two variables cover it:

- `STORAGE_SEED_URL` points at a tarball of a `storage/` directory. On a boot
  with an empty volume, the app downloads and unpacks it once. It refuses to
  overwrite a volume that already has content, so it cannot destroy live data.
- The Azure Blob variables make the app mirror `storage/` to blob storage and
  restore from it at boot.

---

## When something is wrong

| Symptom | Look at |
| --- | --- |
| Will not start | `python -m scripts.setup --check`. It stops at the first blocking problem and names it. |
| `/readyz` reports the store down | `COSMOS_URI` and `COSMOS_KEY`. The response body breaks it down per dependency. |
| Answers stream slowly then arrive at once | Proxy buffering. See the nginx block above. |
| An answer cites the wrong clause | Re-run extraction for that document, then `rebuild_kb`. If it persists, that is a defect worth reporting: evidence fidelity is the thing this project is for. |
| Highlights vanish on a table | That document skipped a pipeline stage. Re-run extraction for it, which is free if the cache is intact, then `rebuild_kb`. |
| A frontend change is not showing | The web bundle is baked into the image. Rebuild it. |
