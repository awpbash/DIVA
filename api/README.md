# api/: FastAPI backend

Multi-turn, streamed Q&A over the Cosmos DB knowledge store built by `pipeline/`,
plus the application layer around it: passwordless session auth + RBAC, the
multi-reviewer field-verification workflow (approve / reject / amend with a
majority consensus), the aligned-knowledge (KM) endpoints, admin (accounts,
registry, upload→extract, usage and activity tracking, feedback inbox), and
server-side chat threads. In the single-container image the app also serves the
built React SPA (catch-all route registered after every API route).
Retrieval is an **agentic tool-calling loop over store queries**. Cross-document
work runs as deterministic queries + exact Python arithmetic, not by stuffing
raw text into a prompt.
Every answer carries `[ev:…]` citations that resolve to exact PDF paragraph
highlights, and a citation guard rejects any id the store didn't return.

Auth: the SPA logs in via `POST /auth/login` (passwordless, accounts in SQLite
`storage/app.db`, the first one created on boot from `BOOTSTRAP_ADMIN_EMAIL`) and
sends the minted session as an `X-User-Token` header. Roles (`admin` / `confidential` / `default`)
are **derived from the session server-side** (clients never pass a role) and
drive redaction and route gating everywhere.

## Layout

| Path | Role |
|---|---|
| `main.py` | FastAPI app + lifespan (async Cosmos store + OpenAI client), CORS, router wiring, health endpoints (`/healthz`, `/readyz`), SPA serving, optional API-key gate. |
| `settings.py` | Tunable knobs (top_k, model names, CORS origins), wraps `pipeline.config.Config`. |
| `deps.py` | Singletons: the async Cosmos store (`pipeline.store.aio`) + `openai.AsyncOpenAI`. |
| `appdb.py` | SQLite (`storage/app.db`): accounts, sessions, chat threads, feedback. The first admin is created on boot from `BOOTSTRAP_ADMIN_EMAIL`. |
| `boot_seed.py` | Seed-on-empty boot shim for cloud deploys: downloads a seed tarball into an empty storage volume once, then hands off to uvicorn. Local dev never runs it. |
| `review_votes.py` | Multi-verifier vote store + consensus: append-only votes in `storage/app.db`, per-value buckets, correction votes carry their own evidence. |
| `trust.py` | Per-document review/trust tier (how much of a doc is human-verified) for badges + catalog. |
| `rag/planner.py` | LLM intent classifier, turns the question into an `IntentPlan` (intent + key terms + category hints). |
| `rag/tools.py` | The tool surface the agent can call (vector/keyword search, store lookups, aggregation, supersedence via `lookup_current_value`, and the KM tools `lookup_verified_fields` / `aggregate_ops_fields`), with RBAC applied at source. |
| `rag/agent.py` | The tool-calling loop: dispatches the model's tool calls in parallel, unions citations into an evidence bundle, yields `AgentEvent`s for streaming. |
| `rag/synth.py` | Streams the final markdown answer, constrained to the retrieved bundle, emitting `[ev:id]` markers. |
| `rag/citations.py` | `[ev:id]` parser + validator + token-safe SSE forwarder (a tag can't be split across events). |
| `rag/policy.py` | View-time sensitivity policy (configs/policy/sensitivity.yaml): answer filtering + citation tagging by role. |
| `rag/resolve.py` | Entity → document resolution for scoping ("the Fairhaven agreement" → doc_ids). Its stopword vocabulary is domain config, read from the ontology's `identity.generic_tokens`. |
| `rag/usage.py` | Per-request OpenAI token accounting (`TokenMeter` threaded through planner, agent, synth). |
| `rag/prompts.py` | Single source of truth for the planner / agent / synth prompts. |
| `rag/schemas.py` | Pydantic models shared between routes and rag (`Citation`, `IntentPlan`, `GraphPayload`, …). |
| `routes/chat.py` | `POST /chat`: SSE stream (see below), effective role from the session. |
| `routes/auth.py` | Passwordless login, session mint/check, account list. |
| `routes/threads.py` | Server-side chat-thread store (per user). |
| `routes/docs.py` | `GET /documents` (with review-tier + family metadata), `GET /pdf/{doc_id}`. |
| `routes/evidence.py` | `GET /evidence/{evidence_id}`: page, rects, snippet for a highlight. |
| `routes/graph.py` | `GET /graph/subgraph` (per-answer evidence tree) + `GET /graph/overview` (Explore tab), clearance-gated. |
| `routes/review.py` | Field-verification workflow: review records, page images, multi-reviewer votes (approve / reject / amend) resolved by majority consensus, propagates verified values to the KM layer. Open to approved verifiers (admins or accounts an admin flags). |
| `routes/km.py` | Knowledge tab: aligned OpsFields, document DAG, per-field currency. |
| `routes/ontology.py` | Admin CRUD over the field schema overlay (SQL rows over the active domain's `configs/views/<domain>_ops.yaml`). |
| `routes/registry.py` | Admin-only named document families: the folders behind the upload form's dropdown, backed by `pipeline/kb/registry.py`, attributed mutations, retire instead of delete. |
| `routes/admin.py` | Admin dashboard: overview, account management (incl. the verifier flag), PDF upload + background extract jobs, usage metrics, and an activity feed. |
| `routes/feedback.py` | User feedback capture + admin inbox. |
| `routes/policy.py` | Sensitivity policy introspection. |

## Run

```bash
# from repo root, with the Cosmos emulator reachable (COSMOS_* in .env)
.venv/Scripts/python -m uvicorn api.main:app --reload --port 8000
```

`http://localhost:8000/docs` for the Swagger UI. In Docker, `docker compose up -d`
brings up the Cosmos emulator (:8081) and the app container (API + SPA, :8000,
also mapped to :8080).

## Chat: request → SSE response

```
POST /chat
{
  "messages": [{"role": "user", "content": "What is the consumption charge rate?"}],
  "doc_ids":  ["b9dce95774d8b97e"]      // optional scope filter
}

Response: text/event-stream
  event: plan          the planner's IntentPlan
  event: step          one iteration of the agent loop
  event: tool_call     a retrieval tool the agent invoked
  event: tool_result   that tool's result (incremental citations)
  event: agent_done    retrieval finished, final evidence bundle assembled
  event: citations     the resolved citation set for the answer
  event: token         a streamed synth delta  (many)
  event: done          { used_citations, unknown_citations }
  event: error         { message }            (on failure)
```

The synth pass emits `[ev:<evidence_id>]` inline. The frontend renders each as a
clickable badge that resolves to a paragraph highlight on the PDF panel.

## Retrieval architecture (graph-first agent)

1. **Planner** classifies the question into an intent (factual, comparison, formula,
   clause, cross-ref, aggregation, definition, …) and lifts key terms + category
   hints, a head start on which tool to reach for.
2. **Agent loop** (`agent.py`) seeds a tool-calling model with that plan and iterates:
   the model emits tool calls, they run in parallel, their citations union into a
   bundle keyed by `evidence_id`, and the loop repeats until the model calls
   `finish` (or `max_steps`). The tools (`tools.py`) include vector search over
   evidence / section / block embeddings, keyword search, section +
   defined-term lookups, label-filtered fact queries, **count / aggregate** over
   facts (for cross-doc sums and comparisons), cross-reference expansion,
   supersedence-aware current-value lookup, and the KM tools over the verified
   aligned fields (`lookup_verified_fields`, `aggregate_ops_fields`), all
   sensitivity-filtered at source by the session role.
3. **Synth** (`synth.py`) streams a markdown answer constrained to the final bundle.
   Every claim must carry a `[ev:id]` pointing at a retrieved span.
4. **Citation guard** (`citations.py`) validates cited ids against the bundle and
   reports any hallucinated id in the `done` event.

Retrieval (cheap, iterative) and synthesis (paid once, streamed) are kept separate,
so the agent can probe the store freely before the single streaming answer.

## Graph endpoints

- `GET /graph/subgraph`: given a set of evidence ids, returns the per-answer
  evidence tree (fact → evidence → section → agreement → document) for the inline
  subgraph beside each answer.
- `GET /graph/overview`: a paginated, fully-linked knowledge graph for the Explore
  tab. The document spine, typed facts with provenance folded on (page, snippet),
  deterministically-derived fact-to-fact edges, identity hubs, and quarantined
  Proposal nodes. Edge types are a closed allow-list, nothing invented.
- `GET /graph/legend`: how the Explore graph groups and colours node labels,
  built from the active pack so the frontend holds no copy of any one domain's
  label list.

## Why CORS, not a Vite proxy

The dev frontend hits the API directly with CORS allowed for the Vite origins.
Vite's bundled proxy doesn't pass FastAPI's SSE stream reliably. The direct path
keeps streaming identical to production.
