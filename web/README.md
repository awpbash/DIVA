# web/: Vite + React + TS frontend

A login-gated workspace over the engineering knowledge base. The core is the chat
tab (chat on the left, the source PDF on the right) plus, by role: **Explore**
(the knowledge graph itself), **Review** (per-document field verification with
green/yellow/red triage, on-page highlights, and multi-reviewer approve/reject/amend
consensus), **Knowledge** (the aligned cross-document fields, DAG + current
values), **Ontology** (admin-only field-schema editing), and **Admin** (accounts,
registry, upload→extract, usage and activity, feedback inbox). Every answer's
citations resolve to a paragraph highlight on the PDF. Every graph node resolves
to its provenance. Nothing is shown that the backend didn't return: role
redaction happens server-side.

## Run

```bash
# from repo root, with the API running on :8000
cd web
npm install
npm run dev          # http://localhost:5173
```

`npm run build` typechecks (`tsc --noEmit`) and bundles with Vite. Override the
backend with `VITE_API_BASE` (unset: dev server → `http://127.0.0.1:8000`,
production build → same-origin relative paths, served by the FastAPI app itself).
In Docker, `docker compose up -d` bakes the built SPA into the single app image
and FastAPI serves it on :8000 (also mapped to :8080). A frontend change needs
`docker compose build app` to show up in Docker.

## Stack

| Dep | Why |
|---|---|
| `react` + `react-dom` | UI |
| `vite` + `@vitejs/plugin-react` | dev server + bundler |
| `react-pdf` + `pdfjs-dist` | renders the PDF, we overlay highlight rectangles ourselves |
| `react-markdown` + `remark-gfm` | streams markdown (with GFM tables) into the chat bubble |
| `reactflow` | canvas for both graph views (inline subgraph + Explore) |

No router, no state library: `useState` in `App.tsx`, a few hooks, and a
small auth context (`auth.tsx`) are enough for a single tabbed page. The source
tree is TypeScript-only (`tsc` is typecheck-only, Vite does the bundling), so no
compiled `.js` lives in `src/`. Note: `vite.config.ts` pins
`minifyIdentifiers: false` to dodge a bundler lexer bug, do not remove it.

## Layout

```
src/
├── App.tsx                tab shell + two-pane chat workspace, owns scope + focused evidence
├── api.ts                 fetch + hand-rolled SSE consumer (EventSource is GET-only), X-User-Token header
├── auth.tsx               session context: login state, role/capabilities from the server
├── rbac.ts                role → visible-tabs/capability mapping (mirrors the server's gates)
├── docmeta.ts             shared document-metadata helpers (families, review tier)
├── types.ts               hand-mirrored pydantic models from api/rag/schemas.py
├── styles.css             all CSS (design tokens at the top)
├── hooks/
│   ├── useThreads.ts      server-backed chat threads (persisted per user via /threads)
│   └── useAdminJobs.ts    polls /admin/jobs for extraction/refresh job state
└── components/
    ├── LoginScreen.tsx    passwordless account picker (seeded demo accounts)
    ├── Sidebar.tsx        left rail: new chat, thread list, document library, tab nav
    ├── DocCatalog.tsx     document library grouped by family, with review-tier badges
    ├── ChatPanel.tsx      message list + composer + plan strip + auto-scroll
    ├── AssistantMessage.tsx  markdown rendering with inline citation badges + report-answer
    ├── CitationBadge.tsx  §section / page chip (+ human-verified ✓), click → PDF highlight
    ├── PdfPanel.tsx       react-pdf single-page view + bbox overlays
    ├── PageJump.tsx       numeric page box inside every pager (type a number, Enter jumps)
    ├── InlineSubgraph.tsx per-answer evidence slice (pre-expanded GraphCanvas)
    ├── GraphCanvas.tsx    layered evidence-tree canvas (Doc → Section → Fact → Evidence)
    ├── ExploreCanvas.tsx  node-link KG renderer for the Explore tab (expand-to-drill, minimap)
    ├── GraphExplorer.tsx  Explore tab: ExploreCanvas + legend/filter + inspector + PDF
    ├── GraphInspector.tsx explainability side panel (node provenance / edge justification)
    ├── ReviewView.tsx     field-verification tab: page image + fields-by-category (+ ReviewDocList, ReviewHeatmap)
    ├── KnowledgeView.tsx  aligned cross-document fields: DAG, current values, trust tiers
    ├── OntologyView.tsx   admin-only field-schema editor (SQL overlay over the view yaml)
    ├── AdminDashboard.tsx admin overview: accounts, registry, upload→extract jobs, usage + activity, feedback inbox
    ├── JobTicker.tsx      app-level strip showing the running extraction/refresh job on every tab
    ├── HelpButton.tsx     floating "How to use" guide modal (annotated screenshots, role-filtered)
    ├── WarmupBanner.tsx   calm cold-start notice while a sleeping demo server wakes
    ├── FeedbackButton.tsx floating feedback widget (+ per-answer report event)
    ├── AuthedImage.tsx    session-authed page-image fetch (review/KM page PNGs)
    ├── graphTheme.ts      label → colour/group/legend mapping + proposal trust check
    └── Icon.tsx           inline SVG icon set
```

## Two graph views

- **Inline subgraph** (`InlineSubgraph` → `GraphCanvas`): the evidence tree behind a
  single answer, shown beside the chat bubble. Derived from `/graph/subgraph`.
- **Explore tab** (`GraphExplorer` → `ExploreCanvas`): the whole knowledge graph from
  `/graph/overview`: document spine, typed facts, derived fact-to-fact edges,
  cross-document identity hubs, and quarantined Proposal nodes. Expand a section
  to drill in (no hairball). The legend comes from the active pack (`/graph/legend`)
  and doubles as a per-type show/hide filter.

### Explainability + trust
Clicking a node opens `GraphInspector`: its properties plus provenance (source page,
snippet, evidence count) and a **View highlight in PDF** button that reuses the same
citation/bbox machinery as chat. Clicking an asserted identity edge shows
*why* it was asserted (`method` / `confidence` / `signals`). Proposal and otherwise
unverified nodes are visually flagged. Confidence is shown where the store records it.
When provenance isn't recorded, the panel says so plainly rather than guessing.

## Citation → highlight roundtrip

The synth LLM emits `[ev:<evidence_id>]` inline. `AssistantMessage` tokenises every
rendered text node (paragraphs, list items, table cells, headings) so a badge appears
wherever a tag is. Clicking a badge sets the focused evidence in `App.tsx`. `PdfPanel`
jumps to that page and draws rectangles for every citation on it, the focused one
rendered most strongly. Boxes are normalised 0–1 in PDF coords. The overlay measures
the rendered canvas via `ResizeObserver` and scales to match.

> **Testing note:** chat threads persist server-side (`/threads` → `storage/app.db`)
> with their citation geometry. After a backend citation/bbox change, start a
> **fresh chat thread** to see it: old threads replay the cached coordinates. The
> Explore tab's PDF jump is independent of chat threads.

## SSE: why hand-rolled

`EventSource` is GET-only, but `POST /chat` is the natural shape (the body carries the
conversation). `api.ts` reads the `text/event-stream` off `fetch().body.getReader()`.
Tag-safe token forwarding is handled server-side (`api/rag/citations.py`) so an
`[ev:…]` tag can't be split across SSE events.
