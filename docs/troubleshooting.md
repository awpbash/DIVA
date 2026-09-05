# Troubleshooting

Start with the symptom that matches your instance. Each entry explains what is
happening before suggesting a fix, so the same pattern is easier to recognize
the next time it appears.

Before anything else:

```bash
python -m scripts.setup --check
```

It reports without changing anything and stops at the first blocking problem,
so it usually names the thing directly.

## Starting up

### Setup stops at "Knowledge store"

The database is not reachable. Start it and wait for it to be healthy, which
takes about a minute the first time:

```bash
docker compose up -d cosmos
docker compose ps          # wait for healthy
```

If it is running and still unreachable, check `COSMOS_URI` and `COSMOS_KEY` in
`.env`. From inside a container the address is the service name, not
`localhost`.

### The app asks me to select a domain

More than one pack is present under `configs/packs/` and nothing declares
which one this instance serves. DIVA asks for an explicit selection so the
active schema remains aligned with the document collection.

Set `VERBATIM_DOMAIN` in the environment, or `domain:` in
`configs/pipeline.yaml`. See [Domains](domains.md#activate).

### Every request returns 401 or 403

Three separate things can do this.

| | |
| --- | --- |
| `CHAT_API_KEY` is set | Every route then requires that value in an `X-API-Key` header. Unset it for local work |
| You are signed in as `default` | Chat only. The knowledge, explore and review surfaces need higher clearance |
| You are signed in but not a verifier | The Review tab needs the approved-verifier flag. `python -m scripts.accounts verifier <email> --on` |

### The login screen rejects the address I expect to work

Accounts are explicit. Only addresses that exist can sign in, and the first
one is created on boot from `BOOTSTRAP_ADMIN_EMAIL`, defaulting to
`admin@localhost`. Check what exists:

```bash
python -m scripts.accounts list
```

## The database

### The emulator data is missing after a Docker restart

Emulator data does not survive container recreation. This is expected for the
development stack because the database is a projection rather than the source
archive:

```bash
docker compose up -d --force-recreate cosmos
python -m scripts.rebuild_kb
```

That restores everything from `storage/`, embeddings included, with no model
calls.

### The emulator loops after an internal database failure

Its internal Postgres needs recovery after an unclean shutdown. Use the same
two commands as above to recreate the emulator and rebuild its projection.

### Queries return nothing but the documents are definitely loaded

Check the active domain matches the domain the documents were extracted
against. Switching `VERBATIM_DOMAIN` without re-extracting leaves you querying
one schema over another schema's data.

```bash
python -m scripts.reconcile_kb      # free, read-only cross-layer audit
```

## Documents and extraction

### A document ingested but has no fields

Look at `storage/canonical/<doc_id>.json`. If it is missing, reading failed
and nothing downstream ran. If it is present but thin, the reader struggled
with the scan quality, and a higher `RENDER_DPI` or `READER=cu` is the lever.

Do not use `tokens.json` to decide whether a document is extracted. It is a
ledger of spend, not a record of state, and it goes stale.

### I changed the schema and the results did not change

The field extraction cache is presence-based. If a result file exists for a
document, it is returned as it is, regardless of whether the text or the
schema has changed since.

That is what makes re-running free. After a schema change or reader change,
force re-extraction so the cached output reflects the current configuration.

### An amendment shows a value for a field it does not establish

A document that changes one clause often names several others in passing
("the minimum commitment and the audit right are unchanged"), and the model
sometimes reads a nearby figure into one of them. Because the newest document
that states a field wins, one invented value can bury the real one.

Review makes this easy to investigate because the evidence beside the value is
visible in context. Correct the field to `Not Stated` when the document does
not establish it. That records the document's silence and lets the walk fall
through to the last document that stated the field.

Two levers reduce how often it happens. Write field hints that say what shape
the value takes, not just what it means ("a money amount with its currency,
never a duration"), and keep the abstention rules in
`configs/prompts/field_extract.md` in front of the model.

### My correction is stuck on "pending concur"

A correction overrides the machine value everywhere, so by default it waits for
a second verifier to agree. With one account on the instance there is no second
verifier and the correction can never land.

Set `REVIEW_CORRECTION_APPROVALS=1` and recreate the container, since a plain
restart does not re-read `.env`:

```bash
docker compose up -d --force-recreate app
```

Leave it at the default of 2 wherever two people actually exist.

### Table highlights are missing on one document

That document skipped the validation stage, so its facts have no row-precise
rectangles to draw. Re-run validation for it and rebuild. Both are free.

### The citation opens the right page but highlights the wrong paragraph

Evidence geometry is central to the verification workflow. Re-anchor the stored
rectangles from their cited blocks:

```bash
python -m scripts.reanchor_evidence
python -m scripts.rebuild_kb
```

Then check in a **new chat thread**. See the next entry for why.

## The application

### I corrected a citation and an old conversation still shows the old highlight

Chat threads are stored server-side with the citation geometry they had when
they were created. An old thread replays its cached coordinates and will keep
showing the old behaviour forever.

After any citation or bounding-box change, test in a new thread so it uses the
updated citation geometry.

### I changed the frontend and nothing changed in the browser

The React workspace is built into the image. The Python source is mounted, so
a restart picks up backend changes, but a frontend change needs a rebuild:

```bash
docker compose build app && docker compose up -d app
```

More generally, a running container can be older than your working tree. Check
the image timestamp and rebuild state before investigating the source further.

### The frontend build fails with a parse error at 1:1

A known interaction between the minifier and a bundled dependency.
`web/vite.config.ts` pins `minifyIdentifiers: false` to prevent the whole
class of failure. Do not remove that line.

### Pages are slow against a real Azure account but fast locally

Two patterns are invisible on the emulator and expensive on a real account.

`SELECT *` on an item kind that carries an embedding reads about sixty
kilobytes per row. Project the fields you actually need.

Fetching items one at a time in a loop costs one network round trip each.
Batch them, and fire independent queries in one concurrent wave.

## Windows

| | |
| --- | --- |
| Set `PYTHONUTF8=1` | Before any command. Encoding errors on contract text otherwise |
| Run scripts as modules | `python -m scripts.rebuild_kb`, never `python scripts/rebuild_kb.py` |
| Do not use `python -c` with variables inside double quotes in PowerShell | It mangles SQL and YAML. Write a file instead |
| PowerShell 5.1 has no `&&` or `\|\|` | Use `;` with an `if ($?)` guard |
| A second process against `storage/app.db` fails with "unable to open database file" while the app container is up | Docker Desktop's Windows bind mount does not reliably support the shared-memory locking SQLite's WAL mode needs across two separate processes. Stop the app first (`docker compose stop app`), run the one-off command with `docker compose run --rm app ...`, then start the app again |

## YAML

| | |
| --- | --- |
| Quote bare `Yes` and `No` | Unquoted they parse as booleans, and your enum silently stops matching |
| Quote any string inside `{...}` | A flow mapping treats a comma as the next entry, so `hint: unverified, awaiting review` silently keeps only the first half |
| Always put a space after a colon | `key:value` is a string, `key: value` is a mapping |
| Never write `AS value` in a Cosmos SQL query | `VALUE` is a reserved keyword and the emulator rejects it as a column alias, which surfaces as a 500. Alias to `field_value` or similar |

## Frequently asked

**Can I use a model other than OpenAI's?**
Yes. Set `OPENAI_BASE_URL` to any OpenAI-compatible endpoint and set the model
names to what it serves. Azure AI Foundry, a gateway, or a server on your own
network all work through the same seam.

**Do my documents leave my network?**
They go to the model endpoint you configure, and nowhere else. If they cannot
leave, host the endpoint yourself.

**Can I run it without Docker?**
Yes for the app. You still need somewhere to put the knowledge store, and the
emulator in Docker is the easy answer.

**How much does it cost?**
Roughly a dollar to read a long contract, once, and small change per question.
Rebuilding the knowledge base, scoring extraction and running the tests are
free and call no model at all.

**Can I run more than one document type in one instance?**
No, by design. One deployment serves one domain, resolved once at startup. Run
a second instance for a second document type.

**Why is sign-in passwordless?**
It is a deliberate default for local evaluation. An email address is the
credential, so a shared deployment should put an authentication layer in front
of the application. [SECURITY.md](../SECURITY.md) and
[Deployment](DEPLOYMENT.md) cover the available options.

**I still need help.**
Open an issue with the output of `python -m scripts.setup --check`, what you
expected, and what happened. [CONTRIBUTING.md](../CONTRIBUTING.md) has the
rest.
