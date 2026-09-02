# Setup wizard: implementation plan

This is a working plan for an in-browser first-run setup wizard, so a non-developer
can take a fresh checkout to a running, working instance without touching YAML,
`.env`, or a terminal (past one initial `docker compose up`).

**This file is meant to be read and updated by whichever Claude session is doing
the work.** Before starting a checkpoint, read the whole file, not just your
phase. Decisions and technical findings recorded here are load-bearing: they
came out of a long scoping conversation and a first research pass, and
re-deriving or silently overriding them wastes the work already done. When you
finish a checkpoint, tick its box and add a dated line to the Progress log at
the bottom. If you discover a decision here is wrong or a technical assumption
doesn't hold, don't just work around it quietly: correct the text in place and
say so in the Progress log, so the next session isn't misled by a stale plan.

## Status

| Phase | What it delivers | Status |
| --- | --- | --- |
| 1 | Wizard shell, boots-without-config, demo path, ready-made-domain-as-is path | Code complete, see 2026-09-02 log entries for what's verified vs. not |
| 2 | Clone-and-edit path (start from a shipped domain, tweak its fields) | Code complete + automated-tested, see 2026-09-02 (reset) log entry |
| 3 | Build-from-scratch path with AI-assisted schema drafting and toggles | Code complete + automated-tested, see the 2026-09-02 (Phase 3) log entry |
| 4 | Docs, dev escape hatch, test coverage | P4.1 (docs) and P4.2 (dev/admin reset) done. P4.3 (test coverage) substantial but not exhaustive |

Update the Status column as work lands: `Not started` -> `In progress` -> `Done`.
If a phase gets partially done then paused, say what's left in a Progress log
entry rather than leaving the table ambiguous.

---

## 1. Why this exists, in one paragraph

Verbatim is a schema-first document Q&A app. Today, standing one up means
editing `.env`, choosing `VERBATIM_DOMAIN`, and (for a new document type)
hand-authoring four interlocking YAML files. That's fine for the maintainer,
wrong for the stated goal of this project: someone non-technical should be
able to clone the repo, run one setup step, and finish everything else by
answering plain-language questions in the browser, ending with their own
documents searchable and cited.

## 2. Decisions already made (don't re-litigate these)

These came out of a question-by-question scoping conversation. Each is a firm
product decision, not a suggestion.

1. **Medium**: the wizard lives in the browser, on first run of the app. Not a
   terminal wizard, not a separate installer.
2. **Pre-boot step stays minimal**: the user still runs something like
   `docker compose up` to get a bare server running. Everything past that is
   in-browser.
3. **`scripts/setup.py` stays as-is**, aimed at developers/CI. The wizard is a
   separate, parallel path for everyone else. They may share underlying
   logic (they do, see section 3) but neither is built in terms of the other.
4. **Boot gating**: the app must boot successfully with *zero* configuration
   (no domain, no admin, no API key). Every route redirects to the wizard
   until setup is complete. This replaces today's hard crash-at-import when
   no domain resolves.
5. **Three domain paths**, in order of how much they ask of the user:
   - **Demo**: loads the shipped `commercial_agreement` domain and the sample
     corpus in `examples/corpus/`. Zero authoring.
   - **Clone-and-edit**: pick an existing shipped domain, then add/remove/edit
     its fields before finishing. (Phase 2.)
   - **From scratch**: describe your documents in plain language (optionally
     upload 1-3 sample PDFs), AI drafts a field list, you edit it by hand,
     then you flip toggles for the advanced structural features. (Phase 3.)
6. **Sensible defaults, hidden by default**: reader (`rapidocr`), database
   mode (local Cosmos emulator, `client` vector mode), and model base URL
   (`api.openai.com`) are never shown on the main path. An "Advanced" section
   can reveal and change them.
7. **API key is live-tested**, one small real model call, before the wizard
   lets the user move on. Reuse the existing test in
   `scripts/setup.py:check_models()` rather than writing a second one.
8. **Admin account**: wizard creates exactly one admin (email + display name).
   It does **not** cover inviting teammates or setting roles for others.
   That stays in the existing admin area, untouched.
9. **Instance name** (`APP_NAME`) is a wizard step. Small, but explicitly
   in scope.
10. **Finishing mechanics**: wizard writes config, the app restarts itself
    (not a hot-reload of a running process), browser shows a short "setting
    up..." wait, then reloads into the real app.
11. **Landing state**: the wizard's last screen *is* the upload step (or, on
    the demo path, a one-click "load the sample contracts" button). Setup and
    getting the first document in feels like one continuous action.
12. **Schema content vs. structure, kept separate on purpose**:
    - **Content** (the field list itself: name, type, hint, example value,
      example source sentence) is drafted by AI from a plain-language
      description plus optional sample PDFs, then edited by hand. Free-form
      generation is fine here because a bad field is cheap to fix (delete it).
    - **Structure** (amendment chains, cross-document party matching, totals,
      confidentiality) is **never freely generated**. Each is a checkbox
      wired to a fixed, pre-tested template. The user (with an AI-suggested,
      user-confirmed guess) only ever points the template at which of *their*
      fields plays which role. This was an explicit correction mid-scoping:
      free-generating the graph/ontology layer is too failure-prone to ship
      to non-developers, so it's templated instead.
13. **Final-config validation failure handling**: quiet automatic retry (bound
    the attempts, e.g. 3), then if still failing, stop and show a
    plain-language error describing what to change (never silently downgrade
    or drop a field/toggle without telling the user).
14. **Delivery**: phased. Build and check in after each phase rather than
    everything in one pass.

## 3. Technical grounding (read before writing code)

Findings from reading the actual code, not assumptions. Re-verify anything
marked **[VERIFY]** before relying on it, since it wasn't proven end-to-end,
only read.

### 3.1 The four-file domain anatomy

A domain is `configs/analyzers/<domain>/analyzer.yaml` (what the reader may
notice), `configs/ontology/<domain>.yaml` (what may exist in the graph),
`configs/packs/<domain>.yaml` (how noticed things become graph nodes),
`configs/views/<domain>_ops.yaml` (the field schema people actually see and
verify). Full detail and a worked example: `docs/domains.md`. Read it in full
before Phase 3, it's the spec the generated files must satisfy.

### 3.2 There's already a live, validated, runtime field editor. Reuse it.

`api/routes/ontology.py` already lets an admin add/edit/delete fields on the
**currently active** domain, without restarting or touching YAML:

- `POST /ontology/field` adds a field. Always gets `source: {mechanism: llm}`,
  meaning it's filled by the model directly from its own `hint` text at
  extraction time. No pack/analyzer/ontology wiring needed for this mechanism.
  Allowed types (`_USER_TYPES` in that file): `text`, `number`, `value`,
  `enum`, `free_text`.
- `PUT /ontology/field/{category}/{key}` edits title/hint/enum values/
  per-field sensitivity.
- `DELETE /ontology/field/{category}/{key}` removes it.
- `PUT /ontology/sensitivity` sets a whole category's RBAC level
  (`general`/`confidential`).
- Every mutation is validated by `compile_with_overlay` (the same drift gate
  that guards hand-authored YAML) against a **copy** before it's persisted, so
  a bad edit never lands. Overlay edits persist as rows via
  `pipeline/kb/ontology_store.py`, merged over the curated base file at load.

**Implication**: the "content" half of Phase 2 and Phase 3 (the plain field
list: name, type, hint) doesn't need new backend machinery. Point the wizard
UI at these exact endpoints. Only the "structure" half (section 3.4 below)
needs anything new.

### 3.3 The `admin@localhost` bootstrap admin already exists on every fresh install

`api/appdb.py`: `init()` calls `bootstrap_admin()` automatically whenever the
users table is empty, creating `admin@localhost` (or
`BOOTSTRAP_ADMIN_EMAIL`/`BOOTSTRAP_ADMIN_NAME` if set) as an admin, and marks
it a verifier. So by the time the wizard's admin-account step runs, this
account **already exists**.

**Implication for the wizard's admin step**: don't create a second admin
alongside it. Either rename this row (update its email/name to what the user
typed, reusing `appdb.upsert_user`) or delete it and insert a fresh one. This
matters for real: `admin@localhost` is a well-known, documented default.
**If the wizard finishes without retiring it, it's a permanent backdoor**
(sign-in is passwordless, so knowing the email is knowing the credential).
Treat "no account still using the default bootstrap email" as a hard
finish-line check, not a nice-to-have.

### 3.4 The domain-resolution crash is the real boot blocker, and it's eager, at import time

`pipeline/ontology.py` line ~71:

```python
DEFAULT_DOCTYPE = resolve_active_doctype()
```

This runs **at module import time**, not inside a function. `resolve_active_doctype()`
raises `RuntimeError` when no domain can be resolved (no `VERBATIM_DOMAIN`, no
`configs/pipeline.yaml` declaration, and zero or more-than-one pack present).
Because this is a module-level statement, merely *importing* `pipeline.ontology`
(which a long transitive chain of other modules does, including things
`api/main.py` imports at startup) crashes the whole process before FastAPI
even constructs the app. This, not the API key, is what makes the app
"refuse to start" today (the API key is already lazy and lenient, see
`pipeline/config.py`: it boots fine with no key and only fails the specific
call that needed one).

**18 files reference `DEFAULT_DOCTYPE`** as of this writing (grep for it to
get the current list; recorded here so you know the blast radius before
starting):
`api/main.py`, `api/rag/policy.py`, `api/rag/prompts.py`, `api/rag/resolve.py`,
`pipeline/extraction/canonical_lite.py`, `pipeline/extraction/ontology_compile.py`,
`pipeline/extraction/pack.py`, `pipeline/ingest.py`, `pipeline/kb/field_llm.py`,
`pipeline/kb/km.py`, `pipeline/kb/load.py`, `pipeline/kb/opsview_spec.py`,
`pipeline/ontology.py`, `scripts/rebuild_kb.py`, `scripts/render_ontology.py`,
`tests/domains.py`, `tests/extraction/test_active_domain.py`,
`tests/rag/test_prompt_neutrality.py`.

Most of these use it only as a **default argument value** (e.g.
`def load_ontology(doctype: str = DEFAULT_DOCTYPE)`), which is evaluated once,
at function-definition time (import time), same problem one level removed.

**Recommended fix, lightest-touch option**: don't refactor all 18 call sites.
Instead change `resolve_active_doctype()` itself to **never raise**: when
nothing resolves, return a sentinel (e.g. `""` or `"__unconfigured__"`) instead
of raising `RuntimeError`. That alone makes every module that merely
*references* `DEFAULT_DOCTYPE` at import time safe, because the assignment
itself no longer throws. The actual `RuntimeError` only needs to fire later,
the first time something tries to genuinely *use* the sentinel (e.g. open
`configs/ontology/__unconfigured__.yaml`, which will raise `FileNotFoundError`,
a fine and correct failure, but one that only happens if something actually
tries to serve a real request against the unconfigured domain). Since the
boot-gate middleware (checkpoint P1.2) stops any such request from reaching
that code before setup is complete, this should never actually trigger in
practice.

**[VERIFIED, 2026-09-02, implemented]**: read all three candidate files.
`pipeline/kb/opsview_spec.py`'s only module-level statement
(`_VIEW_PATH = view_path(DEFAULT_DOCTYPE)`) just builds a `Path`, never
touches disk, so it's safe as-is. `pipeline/extraction/pack.py` has no eager
call at all (`DEFAULT_DOCTYPE` is only ever a cached default argument there).
`pipeline/extraction/ontology_compile.py` DID have a real one
(`_DEFAULT = tables(DEFAULT_DOCTYPE)` at module level, plus the four names
derived from it) — fixed by guarding it with `if DEFAULT_DOCTYPE:`, falling
back to empty dicts/frozenset otherwise.

**A fourth landmine, not on the original suspect list, found only by
actually booting**: `api/rag/tools.py` line ~1281 had
`_LABEL_ENUM = sorted(_id_key_by_label())` at module level. This one does
NOT reference `DEFAULT_DOCTYPE` by name anywhere in the file — it calls
`_id_key_by_label()`, an `@lru_cache`d function that calls `load_pack()`
with **no argument**, silently relying on `pack.load()`'s own
`doctype: str = DEFAULT_DOCTYPE` default parameter. Grepping for
`DEFAULT_DOCTYPE` (which is how the original 18-file list above was built)
cannot find this kind of landmine, because the string never appears in the
file. Fixed the same way (`if DEFAULT_DOCTYPE else []`). **Lesson for
whoever does P1.1 on a similar codebase in future**: grepping a constant's
name is not sufficient to find every eager consumer of it; the only
reliable check is what the acceptance criteria already said — actually
import `api.main` (or boot uvicorn) with a genuinely empty/ambiguous
`configs/packs/` and see what breaks, and keep iterating fix→reboot until
it's clean. Don't skip this step even after the grep-based audit looks
clean. Verified clean end-to-end 2026-09-02 (see Progress log): a simulated
zero-pack checkout imports `api.main` and answers `GET /healthz` with 200.

**[VERIFY] regression risk**: `tests/domains.py` and
`tests/extraction/test_active_domain.py` exist specifically to pin this
resolution behavior down. Expect to update them for the new "returns a
sentinel instead of raising, until asked to actually resolve" contract. Run
the full `python -m pytest tests/` after this change before doing anything
else in Phase 1, this is the highest-regression-risk single change in the
whole project.

### 3.5 The pieces Phase 3's toggles need, and which ones are genuinely hard

- **Confidential fields**: exactly `PUT /ontology/sensitivity` /
  `field_overrides`, already built (section 3.2). Zero new backend work. In
  Phase 3's from-scratch flow, since a whole new ops-view file is being
  written anyway (not an overlay edit), just write the `sensitivity:` block
  directly into it at generation time.
- **Totals / aggregation**: `_USER_TYPES` already includes `value`. Whether
  marking a field `type: value` (with `mechanism: llm`, the only mechanism the
  runtime editor can produce) is *sufficient* for
  `api/rag/tools.py:aggregate_ops_fields` to sum it, or whether that function
  needs a structurally-extracted `mechanism: value` fact (a pack `fact_types`
  entry, like `commercial_agreement`'s `license_fee` field) to get parsed
  `numbers`/`unit`, is **[VERIFY]**, not yet confirmed. Read `_ops_row()` and
  how a stored field's `numbers`/`unit` get populated
  (`api/rag/tools.py`, search near `aggregate_ops_fields`) before building
  this toggle. If `type: value` alone is sufficient regardless of mechanism,
  this toggle is nearly free. If not, it needs the same kind of pack template
  work as party-matching below.
- **Amendment chains**: needs the `document_family` top-level block (
  `document_types`, `relations`, `field_roles`, `ancillary_types`, see
  `docs/domains.md` "The document family policy" and the worked example in
  `configs/views/commercial_agreement_ops.yaml`) plus a
  `document_relationships` category of fields. The **existing runtime admin
  API does not expose editing `document_family`** at all, it only edits
  fields and sensitivity. So this toggle can only be applied at
  domain-*creation* time (writing the initial ops-view file), not as a later
  overlay edit on an already-active domain. Template it as: fixed field set
  (`document_type`, `amendment_ordinal`, `amends_document`, `agreement_date`,
  `effective_date`, matching the shipped example almost verbatim) using
  `mechanism: llm` (not `recital`, which the runtime editor can't produce
  anyway, and `llm` should work fine for this since it's the same "filled by
  the model from its own hint" behavior). The wizard only needs to ask the
  user one thing: what do you call the document types in your corpus
  (defaults to the shipped `Agreement / Amendment / Amended and Restated /
  Assignment / Side Letter / Notice / Other` list, editable).
- **Cross-document party matching (identity hubs)**: the genuinely hard one.
  Needs real content in all three of `analyzer.yaml` (a party category +
  named roles), `ontology.yaml` (`Party` label, `CanonicalParty` hub, the
  `identity:` block: `canonical_roles`, `legal_suffixes`, `generic_tokens`),
  and `packs/<domain>.yaml` (an `organization` `fact_type` mapped to `Party`,
  see `docs/domains.md` section 05 for the exact shape). This is **not**
  reachable through the runtime admin API and can't be, it's structural. Per
  decision #12, **do not let an LLM freely generate this YAML**. Build one
  fixed, parameterized template (the only free variable is the list of role
  names, one per field the user marks as an organization/person name to
  match, defaulting to a slugified version of that field's own title, e.g. a
  field titled "Landlord" becomes role `landlord`) and fill it in
  mechanically. AI's role here is limited to *suggesting which of the user's
  already-defined fields look like organization/person names* (a
  classification over field titles/hints the user already wrote), which the
  user then confirms or corrects, never to writing YAML.

### 3.6 Reusable pieces for the wizard's own steps

- **Live API key test**: `scripts/setup.py:check_models()` already does "one
  paid call that proves the model endpoint answers." Call the same
  underlying logic (refactor it into something both the CLI and the new
  `/setup/*` route can call, rather than duplicating the OpenAI call).
- **Admin creation**: `api/appdb.upsert_user(email, name, title, role, zone)`
  is the primitive. The wizard's admin step is really "update the existing
  bootstrap row to the real admin's details" (see 3.3), not "insert a second
  admin."
- **Available domains list**: `pipeline.ontology.available_doctypes()`
  already returns every domain this checkout ships a pack for. Use it
  directly for the "pick a ready-made domain" list, don't reimplement.
- **Domain resolution / declaration**: `VERBATIM_DOMAIN` env var, or
  `domain:` key in `configs/pipeline.yaml`. The wizard should write one of
  these (prefer `configs/pipeline.yaml`'s `domain:` key over `.env`, since
  `.env` mixes secrets and config and this isn't a secret) as its very last
  step before triggering the restart.
- **Config validation before committing**: `pipeline.extraction.pack.load(domain)`
  and `pipeline.kb.opsview_spec.load(doctype=domain)` are exactly what
  `scripts/setup.py:check_configs()` runs to prove a domain compiles. Run
  these against generated files (in a temp/staging location, or written to
  their real path but with `VERBATIM_DOMAIN` not yet switched over) before
  ever pointing the live app at them.
- **Restart mechanism — [RESOLVED, 2026-09-02, changed from the original
  idea]**: the original idea above (reuse `boot_seed.py`'s `os.execvp`) turned
  out to be the wrong model. `boot_seed.py` execs *before* uvicorn has ever
  bound a socket or served a request — a clean handoff from one process
  image to the next with nothing live yet. Calling `os.execvp` from *inside*
  a running request handler is a different, riskier thing: it replaces the
  process image of a worker that already holds an open listening socket and
  live connections, and getting the new image to either reuse or cleanly
  release that socket before rebinding is exactly the kind of fd/OS-level
  edge case not worth risking for this. Implemented instead
  (`api/routes/setup.py:_trigger_restart`): write the config, respond `200`,
  then on a daemon thread, sleep ~1s and call `os._exit(0)` — a hard,
  unconditional process exit, no cleanup, no exec. This relies on whatever
  is supervising the process to bring a fresh one up, which is true for both
  documented ways to run this app: (a) `docker compose up` — `docker-
  compose.yml`'s `app` service already declares `restart: unless-stopped`,
  so the orchestrator brings up a brand new process reading `.env`/
  `configs/pipeline.yaml` fresh; (b) `uvicorn --reload` (both the compose dev
  command and the plain local-dev invocation in the README) — since the
  wizard writes `configs/pipeline.yaml`, and `configs/` is already a
  `--reload-dir` (compose) or inside the default watched cwd (plain local
  `uvicorn --reload`), uvicorn's own file-watcher typically restarts the
  worker gracefully from the config write alone, often before the 1-second
  delayed `os._exit` even fires — and if the exit fires first, the watcher's
  own pending restart still brings a fresh worker up moments later, since it
  reacts to the file change independently of whether the old worker is
  still alive. Verified end to end with `TestClient` (which doesn't exercise
  real process supervision, only that the endpoint responds `200` before the
  exit call fires on its own thread) — **not yet verified against a real
  `docker compose up` restart or a real bare `uvicorn --reload`**, which is
  the one meaningful gap left in "make sure restart really works" and should
  be a real click-through before calling Phase 1 done.

## 4. Phase-by-phase plan

Each checkpoint below is meant to be independently completable and
verifiable. Tick `[ ]` -> `[x]` as you finish them. Add notes inline (or in
the Progress log) if a checkpoint turned out to need something not listed
here.

### Phase 1 — Wizard shell, boots-without-config, demo + ready-made-as-is

**Goal**: someone can `docker compose up`, open the browser, and reach a
working chat-and-review instance over either the sample corpus or the
shipped `commercial_agreement` domain untouched, entirely through the
browser, no `.env` editing.

- [x] **P1.1 — Boot without a resolvable domain.** DONE 2026-09-02.
  `resolve_active_doctype()` now takes a `required: bool = False` kwarg:
  default returns `""` instead of raising; `required=True` (used by
  `scripts/setup.py:check_domain`) keeps the old raise-and-report behaviour
  exactly. Audited all three originally-suspected files: `opsview_spec.py`
  and `pack.py` needed no change (no real eager call); `ontology_compile.py`
  DID have one (`_DEFAULT = tables(DEFAULT_DOCTYPE)`) and is now guarded
  with `if DEFAULT_DOCTYPE:`. **A fourth file, not on the original suspect
  list, was found only by actually booting**: `api/rag/tools.py` had
  `_LABEL_ENUM = sorted(_id_key_by_label())` at module level, calling
  `load_pack()` with no doctype argument — invisible to a `DEFAULT_DOCTYPE`
  grep since it never names the constant. See section 3.4 for the full
  writeup and the lesson (grep is not sufficient, boot it and see). Updated
  `tests/extraction/test_active_domain.py` for the new contract (added
  `test_ambiguous_checkout_raises_when_required`,
  `test_ambiguous_checkout_returns_sentinel_by_default`,
  `test_no_packs_returns_sentinel_by_default`; the old always-raises test is
  gone, replaced by the `required=True` variant). **Verified**: a simulated
  zero-pack environment imports `api.main` cleanly and `GET /healthz` / `GET
  /branding` both answer 200 (script, not committed —
  `/tmp/vt_boot_check.py` from this session; worth turning into a real
  `tests/api/test_boot_without_domain.py` in P4.3). Full `pytest tests/`
  run: all green except one pre-existing, unrelated failure — see the
  2026-09-02 log entry.

- [x] **P1.2 — Setup-complete gate.** DONE 2026-09-02, with one correction
  to the plan's own design. Explicit flag: `appdb` gained a `settings`
  key/value table (`get_setting`/`set_setting`), `is_setup_complete()`, and
  `mark_setup_complete()`. **The inferred condition changed from what this
  plan originally specified.** The plan said "domain resolves AND at least
  one *non-default* admin exists." Live-testing that literally on this repo
  showed it was backwards: this repo ships exactly one pack (autodetects
  with zero config) and `appdb.init()` always auto-bootstraps an admin on
  any empty database — so "domain resolves + an admin exists" is true on
  *every* boot, wizard or not, and gates nothing; while "a *non-default*
  admin" would 503 the entire existing test suite and every dev workflow
  that never renames `admin@localhost`. Fixed by adding a third condition
  that's never trivially true: a real, non-placeholder `OPENAI_API_KEY`.
  Full reasoning is in `api/appdb.py:is_setup_complete`'s docstring — read
  it before touching this again, it explains why each of the three
  conditions is load-bearing. The "retire admin@localhost" rule (section
  3.3) is still enforced, just moved to a hard check inside `/setup/finish`
  rather than this general gate. Middleware: `api/main.py` gained
  `_setup_gate`, placed so the existing `_api_key_gate` (shared-secret)
  still runs first. It's an explicit allow-list of gated API prefixes
  (`_GATED_PREFIXES`) rather than route introspection, so it can never
  accidentally gate the SPA's own static assets (verified — see the
  2026-09-02 log entry's SPA-coexistence check).

- [x] **P1.3 — `api/routes/setup.py`.** DONE 2026-09-02. Built close to the
  suggested surface, with `has_api_key`/`bootstrap_email_pending` added to
  `/setup/status` (the frontend needs both), and `/setup/finish` hard-checks
  domain + real API key + a retired bootstrap admin before marking complete
  — see the route's docstring. `scripts/setup.py:check_models()` was
  refactored into a pure `test_model_key(api_key, base_url, embed_model) ->
  (ok, message)` that both the CLI and `POST /setup/api-key` call — one
  definition of "this key works." `.env` reads/writes go through
  `python-dotenv`'s `set_key`/`dotenv_values` rather than hand-rolled
  parsing. Every mutating route 403s once `appdb.is_setup_complete()` is
  true (`_guard_not_complete`), so these endpoints can't be replayed against
  a live instance. Router registered in `api/main.py`.

- [x] **P1.4 — Frontend wizard shell.** DONE 2026-09-02.
  `web/src/components/SetupWizard.tsx` + `.css`, matching the existing dark
  design system's CSS variables (same tokens `LoginScreen.css` uses — this
  is an extension of an established look, not a new visual product, so no
  new direction was proposed). `App.tsx` checks `GET /setup/status` before
  deciding what to render (wizard / login / main app), failing OPEN on a
  network error so a transient blip can't permanently trap a real instance
  behind the wizard. Steps built: welcome+instance name -> admin account ->
  API key (live-tested, spinner + inline error) -> path choice (Demo /
  Ready-made-as-is; Clone-and-edit and From-scratch are Phase 2/3, not
  shown yet) -> finishing (polls `/healthz` until the restarted process
  answers, then logs the new admin straight in via `useAuth().login()` and
  reloads — no separate "now go log in" step). **Scope cut, flagged rather
  than silently dropped**: the "Advanced" section only exposes the model
  base URL (already covered by `/setup/api-key`'s `base_url` field).
  Reader choice and database/Cosmos connection overrides from decision #6
  were NOT built — there was no backend endpoint for them in this plan's
  own P1.3 surface, and adding one felt like scope creep on a "start
  building" instruction rather than an explicit ask. Worth a short
  follow-up if Jun Wei wants those genuinely changeable pre-restart rather
  than left to hand-editing `.env`.

- [x] **P1.5 — Demo path completion.** Code DONE 2026-09-02, **live
  ingestion NOT run** (would need a real `OPENAI_API_KEY` and spends real
  money — not run without asking, per standing instructions). Which domain
  the demo offers, and the exact intake declarations that make the sample
  corpus demonstrate its amendment chain, now live in
  `examples/manifest.py` (`DOMAIN`, `CORPUS`) rather than hardcoded in
  engine code — reading `docs/domains.md`'s and `tests/extraction/
  test_active_domain.py`'s "no engine module names a domain in code" guard
  the hard way: it originally failed on `api/routes/setup.py` hardcoding
  `"commercial_agreement"`, and `examples/` is deliberately outside that
  guard's scanned tree. `api/routes/admin.py` gained `ingest_local_pdf()`,
  extracted from `upload_document`'s body, so the demo loader and a real
  browser upload share one content-addressed store + extraction-job code
  path. `api/main.py`'s lifespan reads-and-clears the
  `setup_load_demo_corpus` flag on boot and runs the three-document ingest
  (base, then both amendments declaring `relation=amends` against the
  base's doc_id, per `examples/README.md`) on a background thread.
  **Before trusting this in front of anyone: run it for real** with a
  working key and confirm all three documents actually reach the KB and
  the amendment-chain questions in `examples/README.md` answer correctly —
  this session only verified the code path structurally (imports resolve,
  the manifest values match `examples/README.md`'s stated document types
  and dates), never against a live model.

- [~] **P1.6 — Phase 1 verification pass.** PARTIALLY DONE 2026-09-02.
  Automated, all green: `pytest tests/` (one pre-existing, unrelated
  failure — see log), `ruff check .`, `cd web && pnpm run build`
  (tsc + vite, clean). Manual, done via `TestClient` (not a real browser or
  a real `docker compose up`): the full status -> instance -> admin ->
  api-key -> domain -> finish -> restart-trigger -> gate-reopens flow,
  including confirming `admin@localhost` is gone and the chosen admin
  exists, confirming setup routes 403 once complete, and confirming a
  built `web/dist`'s static assets stay reachable through the gate while
  API calls 503. **Not done, and the real remaining gap before calling
  Phase 1 genuinely finished**: an actual `docker compose up` from a clean
  checkout, clicking through the wizard in a real browser, confirming the
  restart described in section 3.6 actually happens (not just that the
  `/setup/finish` endpoint responds), and confirming the demo corpus
  ingests and answers correctly with a real API key. None of that was run
  this session — it needs a real OpenAI key and Docker, and actually
  spending on it wasn't asked for yet.

### Phase 2 — Clone-and-edit path

**Goal**: a third path, "start from a shipped schema and tweak it," reusing
Phase 1's plumbing plus the *already-built* `/ontology/*` admin endpoints
(section 3.2), not new backend machinery.

- [x] **P2.1 — Design confirmation: activate-then-edit sequencing.** DONE
  2026-09-02. Built as recommended, with one change: rather than exposing
  the wizard's own admin session against the real `/ontology/*` (which
  would require signing the wizard-created admin in mid-wizard, and `/auth`
  is itself gated shut until setup is complete — a real chicken-and-egg),
  `api/routes/setup.py` gained a thin, still-unauthenticated
  `/setup/ontology/*` passthrough that calls `api/routes/ontology.py`'s
  handler FUNCTIONS directly (not over HTTP) with a stand-in admin dict,
  reusing every byte of validation/persistence logic underneath. Confirmed:
  "activated but still editing" does NOT flip `is_setup_complete()` —
  `/setup/finish` is still a separate, explicit later step
  (`/setup/domain/activate` only restarts; it never marks completion).

- [x] **P2.2 — Field-editing wizard screen.** DONE 2026-09-02. Extracted
  the admin Ontology tab's category rail + field table + add/edit form into
  a new shared `web/src/components/OntologyEditor.tsx` (props: fetch/add/
  edit/delete/setSensitivity callbacks), so `OntologyView.tsx` (admin tab)
  and `SetupWizard.tsx`'s new "edit" step are two thin callers of the exact
  same component, not two UIs. The edit step renders in a wider near-full-
  viewport shell (`.setup__editshell`) instead of the small centered wizard
  card, since the field table genuinely needs the room.

- [x] **P2.3 — Second "finish" step.** DONE 2026-09-02. Reuses
  `/setup/finish` as-is (no new endpoint) — its hard checks (domain + real
  key + retired bootstrap admin) are already satisfied by the time the
  clone-and-edit path reaches it, since the admin/key steps happen earlier
  in the same wizard sequence. It does restart a second time (once at
  `/setup/domain/activate`, once at `/setup/finish`), which is a few extra
  seconds rather than special-casing the mechanism per path.

- [x] **P2.4 — Phase 2 verification.** DONE 2026-09-02, automated rather
  than manual: `tests/api/test_setup_routes.py::
  test_setup_ontology_passthrough_reads_and_edits_the_real_schema` walks the
  wizard to clone-and-edit against this checkout's real shipped domain, adds
  a field, renames it, marks its category confidential, deletes it, and
  confirms each change is visible through the *same* read path — proving
  it's the one real overlay store, not a wizard-only copy (the "shows up in
  the normal admin Ontology tab afterward" claim, checked at the storage
  layer rather than by opening two UIs). RBAC (a `default` account can't see
  a confidential category) was NOT re-tested here — that's existing,
  already-covered behavior of `/ontology`'s own sensitivity system, not
  something this phase changed.

### Phase 3 — Build a schema from scratch, AI-assisted

**Goal**: the hardest path. Plain-language description (+ optional sample
PDFs) -> AI-drafted field list -> hand edits -> toggles for the four
structural features -> validated, generated domain -> restart -> upload
screen. Read section 3.5 in full before starting, it lays out which toggles
are cheap and which are genuinely involved.

- [~] **P3.1 — Description + sample upload UI.** DONE 2026-09-02, scope cut
  deliberately: the description box shipped, the optional 1-3 PDF sample
  upload did not. Decision #5/the plan text both call sample PDFs
  optional, and building a second, pre-domain PDF-text-extraction path
  (the real upload path is tied to an ACTIVE domain's reader/extraction
  chain, which doesn't exist yet at this point in the wizard) would have
  been real, avoidable complexity for a genuinely optional enhancement —
  cut in the spirit of "keep it simple" (explicit instruction this
  session). A "start with a blank schema" link sits next to the
  description box for anyone who'd rather not draft at all. Worth a
  follow-up only if plain-language description alone proves not enough
  signal for the model in practice.

- [x] **P3.2 — AI field-drafting endpoint.** DONE 2026-09-02.
  `POST /setup/schema/draft {description} -> {fields: [...]}`, in
  `pipeline/schema_gen.py:draft_fields()` (the model-calling logic) called
  from `api/routes/setup.py:draft_schema`. New prompt file
  `configs/prompts/schema_draft.md`. Output is a real `json_schema`
  structured response (mirrors `pipeline/kb/field_llm.py`'s own pattern
  exactly — schema, message shape, `token_meter.record`), restricted to
  `_USER_TYPES`, and each field is re-validated/re-slugged on the way back
  (bad or duplicate keys from the model are dropped, not trusted) before
  ever reaching the browser.

- [x] **P3.3 — Field-list edit screen.** DONE 2026-09-02, and it reuses
  more than planned: rather than a second component, `OntologyEditor.tsx`
  (Phase 2's extraction) is reused UNCHANGED, wired to purely local adapter
  functions in `SetupWizard.tsx` (a `useRef`-backed field list; no
  network call, nothing persisted until Generate) instead of the
  `/setup/ontology/*` passthrough clone-and-edit uses. One real
  enhancement was needed and is now shared by both callers:
  `OntologyEditor`'s category picker was a closed `<select>` of existing
  categories, which doesn't work when there ARE no existing categories yet
  (a from-scratch domain's first field). It's now a text input with a
  `<datalist>` of suggestions — pick an existing category or type a new
  one. The backend already allowed creating a category on demand
  (`add_field`'s `setdefault`); this only lifted a frontend-only
  restriction, in both the wizard and the admin Ontology tab.

- [x] **P3.4 — Confidential toggle.** DONE 2026-09-02, PER-FIELD as this
  section originally said (not per-category): a `confidential` flag on
  each local field, written into the generated ops-view's
  `sensitivity.field_overrides` at generation time. No dedicated toggle UI
  beyond the field form's existing sensitivity control — same one
  clone-and-edit's screen already has.

- [x] **P3.5 — Totals toggle.** DONE 2026-09-02 — resolved, and resolved
  to "free," which turned out to mean **no toggle exists at all**. Read
  `pipeline/kb/km.py`'s `parse_numbers()` and where a field's `numbers`
  property gets set (`fields.append({..., "numbers": numbers, ...})`,
  computed from the field's stored VALUE STRING): it runs for every field
  regardless of `mechanism` or `type`, and `aggregate_ops_fields` sums
  whatever it finds. So `type: value` alone is sufficient — the [VERIFY]
  resolves to the optimistic branch, no pack-level `measure`/structured
  extraction needed. Picking "number + unit" in the field form (already
  an option, unchanged) already IS this toggle.

- [x] **P3.6 — Amendment-chain toggle.** DONE 2026-09-02, matching the
  worked example almost verbatim as planned:
  `pipeline/schema_gen.py:_build_ops_view`'s `amendment` branch writes the
  fixed 5-field `document_relationships` category (`document_type`,
  `amendment_ordinal`, `amends_document`, `agreement_date`,
  `effective_date`, all `mechanism: llm` per section 3.5's own
  recommendation) plus the `document_family` block, parameterized only by
  the user's document-type list (a textarea, one per line, defaulting to
  the shipped `commercial_agreement`'s own list). The 5 field keys are
  reserved — `schema_gen.generate()` rejects a user field that collides
  with one, with a clear message, rather than silently overwriting it.

- [x] **P3.7 — Party-matching toggle.** DONE 2026-09-02, and validated
  against the real drift gate on the first attempt (see the 2026-09-02
  Phase 3 log entry). One simplification from the plan's exact wording:
  "AI's role is limited to suggesting which fields look like org/person
  names" is implemented as a **local keyword heuristic**
  (`SetupWizard.tsx`'s `PARTY_HINT_WORDS`, pre-checking fields whose
  title/key/hint contain words like "party", "landlord", "vendor"), not a
  live model call. The user reviews and corrects every suggestion either
  way (per decision #12, the AI's role here is strictly advisory), so a
  free heuristic satisfies the actual requirement without a fourth paid
  endpoint. `pipeline/schema_gen.py`'s `_party_fact_type` /
  `_build_ontology` / `_build_pack`'s `party` branches mirror the shipped
  `commercial_agreement` domain's own `organization` fact type +
  `CanonicalParty` hub, parameterized only by the role list (one per
  flagged field, slugified from that field's own title, exactly as
  decision #12 specifies) — no YAML is ever freely generated for this.

- [x] **P3.8 — Generate, validate, retry, finish.** DONE 2026-09-02, one
  deliberate change from the plan's wording: **no retry loop.**
  `POST /setup/domain/from-scratch` assembles the four files, writes them
  to their real `configs/...` paths, then runs the exact two live
  validators `scripts/setup.py:check_configs()` uses
  (`pipeline.extraction.pack.load()`, `pipeline.kb.opsview_spec.load()`,
  with every relevant `lru_cache` cleared first — a retry after fixing one
  field must not validate against a stale read, see
  `pipeline/schema_gen.py`'s module docstring). On failure: the partially
  written files are deleted (so a rejected attempt never leaves
  `ontology.available_doctypes()` listing a broken domain) and the error
  is shown once, plainly. Decision #13's "quiet automatic retry, ~3
  attempts, feed the error back in" was written for genuinely
  non-deterministic generation (an LLM re-rolling a whole file); nothing
  here is generated freely — every structural piece is a fixed template
  (decision #12) — so retrying the identical template against the
  identical input would fail identically every time. Documented as a
  correction in place, not silently worked around. `configs/ontology/
  <domain>.yaml` is written carefully but is NOT one of the two live
  validators (confirmed by reading both modules' imports) — it matters for
  the offline contract test suite and for a human reading the domain
  later, not for whether the wizard lets it through.

- [x] **P3.9 — Phase 3 verification.** DONE 2026-09-02, automated rather
  than against a real synthetic document pair (see "not verified" below).
  `tests/test_schema_gen.py` runs the exact three scenarios this item
  specifies — no toggles at all, confidential + totals only (i.e. one
  plain field, one `type: value` field), and the full set including
  party-matching + amendment-chain — through the real
  `pack.load()`/`opsview_spec.load()` validators, isolated into a
  throwaway `configs/` tree (never the real repo's) seeded with the real
  `_base.yaml` / `_universal` analyzer / analyzer JSON schema so
  `extends:` still resolves. All three passed validation on the FIRST
  attempt once the generator was written correctly (see the log entry for
  what a live smoke test against the real repo caught before the
  isolated suite existed). `tests/api/test_setup_routes.py` adds two more
  full runs through the actual HTTP route (not just the generator
  directly), plus a validation-failure-cleans-up-after-itself test.

### Phase 4 — Docs, escape hatch, tests

- [x] **P4.1 — Docs.** DONE 2026-09-02. `README.md`'s Quick start now leads
  with the wizard (clone, `cp .env.example .env`, `docker compose up -d`,
  then the four in-browser paths as a table), with the old hand-edit flow
  moved into a new "Advanced: scripted setup, no browser" subsection,
  unchanged in content. `docs/getting-started.md` restructured the same
  way: steps 02-03 now cover install-and-start plus a walkthrough of the
  wizard's own screens (instance name, admin account, live-tested API key,
  the four paths), with the old Configure / Start the database / Check your
  setup / Start the app steps preserved verbatim inside a new "Advanced"
  appendix near the end rather than deleted. The manual per-document
  upload walkthrough (step 04) still exists as-is, since it is the
  teaching moment for how amendment-chain declarations work, and a note
  now points out the Demo path does the same three declarations
  automatically. Added a two-sentence cross-reference near the top of
  `docs/domains.md` pointing at the wizard's "Build from scratch" path as
  the faster alternative to hand-authoring the four files. Also added one
  paragraph to the getting-started advanced appendix documenting the
  `perform_reset()` / Danger Zone reset feature from P4.2, since "how do I
  get back to a clean slate" is a natural question right next to "how do I
  set this up" and P4.2 had shipped without a doc mention anywhere. Not
  done: no link-check or rendering pass in an actual Markdown viewer, and
  the section anchors used for the two internal same-page links
  (`#ask-it-something`, `#04--load-the-sample-contracts`) are hand-written
  `<a id>` tags rather than relying on autogenerated heading slugs, so they
  render correctly regardless of the exact slug algorithm the doc host
  uses.
- [x] **P4.2 — Dev reset.** DONE 2026-09-02, ahead of schedule (explicit
  request, not just this plan's own sequencing) and broader than originally
  scoped: not just "clear the setup-complete flag," a full clean-slate wipe.
  One function, `scripts/reset_dev.py:perform_reset()`, called from two
  front doors:
  - CLI: `python -m scripts.reset_dev` (confirmation prompt; `--yes` to
    skip it, `--wipe-key` to also forget `OPENAI_API_KEY`).
  - In-app: `POST /admin/dev-reset` (admin-only), gated behind
    `VERBATIM_ALLOW_RESET=1` (OFF by default — settles the open question
    below as "opt-in, not production-default") plus a typed confirm phrase.
    A "Danger zone" panel in Admin > Accounts (`AdminDashboard.tsx`) calls
    it, hidden entirely unless the backend reports the flag is on.

  What it wipes and why, in detail (including the one non-obvious finding —
  a bare `rm storage/app.db` alone does NOT reopen the wizard, since
  `appdb.init()` regrows a fresh default admin for free on the next boot
  and the domain + key stay configured, so `is_setup_complete()`'s inferred
  fallback reads True again; clearing `configs/pipeline.yaml`'s `domain:`
  key is the part that actually closes that loophole) is written up in
  `scripts/reset_dev.py`'s module docstring — read it before touching this
  again rather than re-deriving it. App-db rows are DELETEd in place (not
  the file removed), specifically to stay safe to call from a live process
  on Windows, where deleting a file sqlite still has a handle on can raise
  `PermissionError`.

  Both restart mechanisms (`/setup/*`'s and `/admin/dev-reset`'s) were
  unified into one `api/restart.py:trigger_restart()` during this work,
  removing a near-duplicate that would otherwise have been the third copy.

- [x] **P4.3 — Tests (partial).** DONE 2026-09-02 for everything built so
  far: `tests/api/test_setup_routes.py` (13 tests) covers the boot gate,
  the full wizard flow through every hard check in `/setup/finish`, the
  clone-and-edit `/setup/ontology/*` passthrough, and both reset front
  doors — all offline (the model-key test is mocked; the Cosmos step of a
  reset is best-effort and degrades to a warning with nothing running, so
  no live database is required). Still open: this only covers what Phases
  1-2 + the reset feature actually built. A dedicated
  `test_boot_without_domain.py` (turning P1.1's manual `/tmp` script into a
  committed test) is still not done.

## 5. Open questions to settle before or during implementation

Not blocking Phase 1, but flag these to Jun Wei before the phase that needs
them, don't just guess:

- Exact wording/UX for the "Advanced" section in Phase 1 (what's shown, how
  it's labelled).
- ~~Whether the from-scratch path (Phase 3) needs a "save progress and come
  back later" capability, given it's the longest path.~~ **Settled
  2026-09-02: no** ("keep it simple" — explicit instruction). The whole
  field list lives in the browser tab's own memory until Generate; closing
  the tab loses it, same as any other unsaved form. Consistent with P3.1's
  cut sample-upload step: nothing here is more than one sitting's worth of
  work once description-drafting does most of the heavy lifting.
- ~~Whether Phase 4's dev reset should also be reachable in production for a
  legitimate "start over" admin action, or stay strictly dev-only.~~
  **Settled 2026-09-02**: both. It's a real in-app admin action
  (`POST /admin/dev-reset`), not dev-only code, but off by default —
  `VERBATIM_ALLOW_RESET=1` opts a deployment in. Picked as the safer default
  given the request was explicitly for a destructive, no-undo action; revisit
  if that default gets in the way.

## 6. Progress log

Append a dated entry every time you work on this. Keep entries short: what
you did, what you found, what's left. This is the changelog for the plan
itself, not for the product.

- **2026-09-02** — Plan written (this file). No implementation started.
  Scoping conversation covered: wizard medium, boot gating philosophy,
  three domain paths, toggle-vs-freeform decision for structural features,
  branding/admin/finish mechanics, phasing. First research pass done:
  confirmed the runtime field editor already exists and is reusable
  (section 3.2), found the real boot blocker is eager domain resolution at
  import time (section 3.4), confirmed `admin@localhost` auto-bootstraps
  and must be retired by the wizard (section 3.3), confirmed
  `scripts/setup.py:check_models()` is reusable for the live API-key test.

- **2026-09-02 (later same day)** — Phase 1 built and verified apart from
  one real gap (see P1.6). Two design corrections made mid-build, both
  written up in place where they matter most (section 3.4, section 3.6,
  `api/appdb.py:is_setup_complete` docstring, P1.5's note) rather than only
  here — read those before assuming this summary is the whole story:

  1. The "inferred setup complete" rule (P1.2) as originally written
     ("non-default admin") was backwards for this repo's actual shape and
     would have either gated nothing (my first, looser attempt) or broken
     the entire existing test suite (the plan's literal wording) — fixed by
     requiring a real API key as the third condition, found only by
     actually running the flow against this repo's real config, not by
     reasoning about it on paper.
  2. The restart mechanism (section 3.6) changed from "reuse
     `boot_seed.py`'s `os.execvp`" to "write config, respond, then
     `os._exit(0)` on a delay, rely on `restart: unless-stopped` /
     uvicorn's own `--reload` watcher" — execing over a live request-
     serving process has real fd/socket risk that boot_seed.py's
     before-uvicorn-starts exec doesn't.

  Also found a fourth eager-import landmine (`api/rag/tools.py`) that no
  amount of grepping `DEFAULT_DOCTYPE` would have caught — only booting the
  app with a genuinely empty `configs/packs/` surfaced it. See section 3.4.

  **What's done and verified** (TestClient + `pytest` + `ruff` + `pnpm run
  build`, all green apart from one pre-existing unrelated failure —
  `tests/api/test_rbac_matrix.py::test_the_route_table_is_not_empty`, which
  fails on a clean checkout too: `requirements.txt` pins `fastapi>=0.111`
  with no upper bound, today's resolved 0.141.1 changed how `app.routes`
  exposes included routers, and the test's route-enumeration helper wasn't
  updated for it. Not fixed — unrelated to this work, flagged for Jun Wei
  rather than fixed unasked):
  - Boot-without-domain (P1.1), the setup gate + inferred/explicit
    completion (P1.2), all `/setup/*` routes (P1.3), the frontend wizard
    for the Demo and Ready-made-as-is paths (P1.4), and the demo-corpus
    ingestion code path (P1.5, structurally — see below).

  **What's explicitly NOT done / NOT verified**, so the next session
  doesn't assume otherwise:
  - No real `docker compose up` + browser click-through. No real restart
    (Docker or `uvicorn --reload`) was observed — only that `/setup/finish`
    responds `200` and calls the exit hook.
  - No live model call anywhere in this session (the API-key test route,
    and the demo corpus's actual extraction) — no `OPENAI_API_KEY` was
    available and spending on one wasn't asked for. Both are coded and
    structurally reviewed, neither is proven against a real model.
  - The wizard's "Advanced" section only covers the model base URL, not
    reader choice or database connection overrides (decision #6's fuller
    scope) — see P1.4's note for why this was cut rather than built.
  - No new automated tests were added for `/setup/*` itself (that's
    P4.3's job); today's coverage of it is the manual `TestClient` scripts
    from this session, which weren't committed to `tests/`.

  Files touched this session: `pipeline/ontology.py`,
  `pipeline/extraction/ontology_compile.py`, `api/rag/tools.py`,
  `scripts/setup.py`, `tests/extraction/test_active_domain.py`,
  `api/appdb.py`, `api/routes/setup.py` (new), `api/routes/admin.py`
  (added `ingest_local_pdf`), `api/main.py`, `examples/manifest.py` (new),
  `web/src/api.ts`, `web/src/App.tsx`, `web/src/components/SetupWizard.tsx`
  (new), `web/src/components/SetupWizard.css` (new). Also created a
  `.venv` (Python 3.13) and ran `pnpm install` under `web/` — neither
  existed in this checkout before this session.

  **Suggested next step for whoever picks this up**: don't start Phase 2
  yet. Close P1.6's real gap first — an actual `docker compose up` (or
  local `uvicorn --reload` + `pnpm run dev`) with a real API key, clicked
  through in a real browser, confirming the restart really happens and the
  demo corpus really answers the ten questions in `examples/README.md`
  correctly. That's the one thing standing between "looks right on paper
  and in TestClient" and "actually works."

- **2026-09-02 (Phase 2 + reset)** — Told to continue past Phase 1's
  suggested pause (above) directly into Phase 2 and onward, plus build a
  clean-slate reset ("nuke button" / auto-reopen-the-wizard), explicitly
  requested rather than waiting for P4.2's original place in the sequence.
  Built and automated-tested: Phase 2 (clone-and-edit, P2.1-P2.4) and P4.2
  (dev reset, both front doors). Stopped before Phase 3 on purpose — it's
  the largest remaining phase, it has real unresolved design questions the
  plan itself says to raise rather than guess at (section 5), and decision
  #14 is phased delivery with a check-in after each phase, not one
  unbroken pass through everything.

  **Design decisions made while building, not just following the plan
  verbatim** (each also written up in place, see P2.1/P2.3/P4.2 above and
  `scripts/reset_dev.py`'s docstring — read those for the full reasoning,
  this is the short version):
  1. Clone-and-edit's field editor talks to a NEW unauthenticated
     `/setup/ontology/*` passthrough, not the real authenticated
     `/ontology/*` directly. The plan's "styled wrapper around the real
     endpoints" idea was right, but signing the wizard's own admin in
     mid-wizard to reach `/ontology/*` doesn't work: `/auth/login` is
     itself gated shut until setup is complete (same gate that protects
     everything else pre-setup), so there is no session to have yet.
     Solved by calling `api/routes/ontology.py`'s handler FUNCTIONS
     directly (not over HTTP) with a stand-in admin dict — same validation,
     same storage, zero duplicated business logic, just no real session.
  2. A bare "wipe the app database" is NOT sufficient to reopen the
     wizard, and this is not obvious going in: `appdb.init()` regrows a
     fresh default admin for free on every boot of an empty database, and
     if the domain declaration and API key are untouched,
     `appdb.is_setup_complete()`'s inferred fallback (added in the Phase 1
     session) reads True again immediately. The reset has to ALSO clear
     `configs/pipeline.yaml`'s `domain:` key for "delete the state and the
     wizard comes back" to actually be true — confirmed by a test that
     deliberately re-runs `appdb.init()` after a reset and checks the gate
     stays open. `tests/api/test_setup_routes.py::
     test_perform_reset_wipes_state_and_the_wizard_reopens` pins this down.
  3. App-database rows are wiped with `DELETE FROM <table>` per table
     (table names read from `sqlite_master`, not hand-listed), not by
     deleting `app.db` itself — deleting a file a live process might still
     hold open is a real `PermissionError` risk on Windows (this
     developer's own OS), whereas truncating rows through the same
     sqlite3 connection pattern the rest of `api/appdb.py` already uses
     has no such risk and works identically whether called from the CLI
     or from a live request handler.
  4. Extracted `api/restart.py:trigger_restart()` out of
     `api/routes/setup.py` (which had it as a private, undocumented-as-
     shared function) so `/admin/dev-reset` doesn't become a third
     near-duplicate of "sleep briefly, then `os._exit(0)`." One place now
     owns "how a restart actually happens."
  5. `/admin/dev-reset` defaults OFF (`VERBATIM_ALLOW_RESET=1` to enable) —
     this settles the plan's own open question (section 5) about dev-only
     vs. production reachability as "both, opt-in." A destructive,
     no-undo action defaulting to reachable felt like the wrong default
     even though it was explicitly asked for; flagged here rather than
     silently deciding it.

  **Verified**: `pytest tests/` (same one pre-existing, unrelated failure
  as the Phase 1 log entry — `test_the_route_table_is_not_empty`, still not
  fixed, still not this session's to fix), `ruff check .` clean,
  `cd web && pnpm run build` (tsc + vite) clean. New test file
  `tests/api/test_setup_routes.py`, 13 tests, all green: the boot gate, the
  full wizard flow through every `/setup/finish` hard check, the
  clone-and-edit ontology passthrough (add/rename/mark-confidential/delete
  a real field against this checkout's real shipped domain), and both
  reset front doors (direct `perform_reset()` call and the `/admin/
  dev-reset` route, enabled and disabled). No live model or database calls
  anywhere in the new tests — `scripts.setup.test_model_key` is mocked, and
  the Cosmos step of a reset degrades to a captured warning rather than
  failing when nothing is listening.

  **Not done / not verified**: same real-browser gap as the Phase 1 entry
  (no `docker compose up` click-through), still open. The clone-and-edit
  UI itself (the wider `.setup__editshell` layout, the reused
  `OntologyEditor` inside it) was checked by `tsc`/`vite build` and the
  automated backend flow, never actually looked at in a browser. `scripts/
  reset_dev.py`'s CLI was smoke-tested for argument parsing
  (`--help`) only, never run for real against a populated instance — doing
  that would delete real local state, so it wasn't run without asking.

  Files touched this session: `api/routes/setup.py` (added the
  clone-and-edit passthrough + `/setup/domain/activate`), `api/routes/
  ontology.py` (unchanged, only imported from), `api/routes/admin.py`
  (added `/admin/dev-reset`), `api/restart.py` (new, extracted),
  `api/main.py` (unchanged this session), `scripts/reset_dev.py` (new),
  `web/src/components/OntologyEditor.tsx` + `.css` (new, extracted from
  `OntologyView.tsx`), `web/src/components/OntologyView.tsx` (now a thin
  wrapper), `web/src/components/OntologyView.css` (deleted, superseded),
  `web/src/components/SetupWizard.tsx` + `.css` (clone-and-edit step),
  `web/src/components/AdminDashboard.tsx` + `.css` (danger zone),
  `web/src/api.ts` (setup-ontology + dev-reset calls),
  `tests/api/test_setup_routes.py` (new).

  **Suggested next step**: raise the two still-open Phase 3 questions from
  section 5 (Advanced-section UX was Phase 1's, already noted as cut; the
  from-scratch save-progress question and the party-matching template's
  scope are Phase 3's) before starting it — Phase 3 is bigger than Phases 1
  and 2 combined and touches a genuinely hard, never-freely-generated YAML
  template (section 3.5), worth a real check-in rather than a guess.

- **2026-09-02 (Phase 3)** — Asked the from-scratch save-progress question
  from section 5; told "no, keep it simple — go straight to Phase 3." Built
  and automated-tested the whole phase (P3.1-P3.9) in one pass, with one
  scope cut (sample-PDF upload, P3.1) and one plan correction (no retry
  loop, P3.8) made along the way and written up in place — see each P3.x
  item above and `pipeline/schema_gen.py`'s module docstring for the full
  reasoning, this is the short version.

  **The one finding that shaped everything else**: before writing any
  generation code, read `pipeline/kb/opsview_spec.py:compile_view` and
  `pipeline/kb/field_llm.py` directly rather than assuming from the plan's
  own wording. `mechanism: llm` fields (what every AI-drafted or hand-typed
  field becomes) need ZERO structural wiring in the analyzer, ontology or
  pack — confirmed by the validator's own per-mechanism branch having no
  case for `llm` at all. That single fact is why a from-scratch domain with
  no toggles at all only needs a near-empty skeleton of the other three
  files, and why the confidential and totals toggles turned out to need no
  generation logic beyond what the field FORM already exposes (a
  sensitivity flag, a `value` type option) rather than separate machinery.
  Also resolved section 3.5's own [VERIFY] the same way — by reading
  `pipeline/kb/km.py:parse_numbers` directly rather than guessing — to
  "`type: value` alone is enough, regardless of mechanism."

  **Verification order matters here, and is worth recording**: rather than
  writing the generator against my own reading of `docs/domains.md` alone
  and hoping, I read the shipped `commercial_agreement` domain's actual
  four files in full first (the doc says to — "the thing to copy") and used
  them as the literal template source. Then, before writing the pytest
  suite, ran the generator against three real scenarios and validated them
  with the REAL live validators against a temporary copy written into the
  real repo's `configs/` tree (cleaned up immediately after) — the same
  "boot it and see" lesson from Phase 1's P1.1, applied here to config
  generation instead of domain resolution. All three passed on the first
  attempt. Only after that did the isolated, committed pytest suite get
  written (into a throwaway `configs/` tree, never the real repo — see
  `tests/test_schema_gen.py`'s fixture), which caught a real test-fixture
  bug of its own (an accidental path collision between the fake
  `storage_root` and the fake `configs/pipeline.yaml`, not a bug in the
  generator) before landing.

  **Verified**: full `pytest tests/` green (same one pre-existing,
  unrelated failure as every prior entry — `test_the_route_table_is_not_
  empty`, still not this session's to fix), `ruff check .` clean,
  `cd web && pnpm run build` clean. New: `tests/test_schema_gen.py` (12
  tests — the three P3.9 scenarios plus field-list/toggle validation edge
  cases, all against real `pack.load()`/`opsview_spec.load()` in an
  isolated `configs/` tree) and 6 new tests appended to `tests/api/
  test_setup_routes.py` (the draft endpoint mocked, two full generate-
  and-validate runs through the real HTTP route, one proving a validation
  failure cleans up after itself). No live model call anywhere in the
  suite — `pipeline.schema_gen.draft_fields` is mocked at the route-test
  level, matching how the API-key test route was already handled.

  **Not done / not verified**: no real browser click-through of the new
  UI (the toggle panels, the local field editor, the "start blank" link) —
  checked by `tsc`/`vite build` and the automated backend flow only, same
  caveat as every other UI built this session. No real, paid model call to
  `/setup/schema/draft` — the prompt file and the JSON-schema response
  shape are written and structurally reviewed, never proven against a real
  model's actual output quality (does it draft good field hints from a
  real description?). No test against a genuinely uploaded, extracted
  2-document amendment pair on a from-scratch domain end to end (P3.9 asks
  for this specifically) — what's verified is that the generated CONFIG
  validates, not that a real document extracts correctly against it; that
  needs a real model call and wasn't run without asking.

  Files added this session: `pipeline/schema_gen.py`,
  `configs/prompts/schema_draft.md`, `tests/test_schema_gen.py`. Files
  changed: `api/routes/setup.py` (added `/setup/schema/draft`,
  `/setup/domain/from-scratch`), `web/src/components/OntologyEditor.tsx`
  (category datalist), `web/src/components/SetupWizard.tsx` + `.css`
  (from-scratch path), `web/src/api.ts` (draft/generate calls),
  `tests/api/test_setup_routes.py` (from-scratch route tests).

  **Suggested next step**: P4.1 (docs) is the one remaining un-started
  item in the original four-phase plan. Beyond that, the real gap across
  every phase built so far is the same one, repeated: nobody has clicked
  through any of this in an actual browser with a real `docker compose up`
  and a real model key. That's worth doing before calling the wizard done,
  not another phase of code.
