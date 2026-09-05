# `web/`

The React client is a single workspace with chat, document review, knowledge,
graph, and admin views. It uses the API for data and session state; it does not
make policy decisions about what a user may see.

## Run it

Start the API on port 8000, then from this directory:

```bash
pnpm install --frozen-lockfile
pnpm run dev
```

Open <http://localhost:5173>. Set `VITE_API_BASE` when the API is not at the
development default. In the Docker image, Vite builds the client and FastAPI
serves the result from port 8000.

Useful checks:

```bash
pnpm run build
pnpm test
```

## Main dependencies

| Package | Use |
| --- | --- |
| React and TypeScript | Interface and type checking |
| Vite | Development server and production build |
| `react-pdf` and `pdfjs-dist` | Render source pages and evidence rectangles |
| `react-markdown` and `remark-gfm` | Render streamed answers |
| `reactflow` and `elkjs` | Layout and display graph views |
| Vitest and Testing Library | Component tests |

## Source layout

```text
src/
├── App.tsx              tabs, layout, and focused evidence
├── api.ts               API requests and POST /chat SSE parsing
├── auth.tsx             session state
├── rbac.ts              server-provided capabilities mapped to the UI
├── types.ts             API response types
├── styles.css           design tokens and component styles
├── hooks/               thread and admin-job hooks
└── components/          chat, PDF, graph, review, knowledge, and admin views
```

`Icon.tsx` contains the inline SVG icons used by the interface. Keep new icons
in that set so the UI has one consistent visual language.

## Evidence display

Chat messages contain citation tags such as `[ev:evidence-id]`. The client turns
those tags into citation buttons. Selecting one loads `/evidence/{id}`, jumps
the PDF to the returned page, and scales the returned rectangles to the rendered
page.

The server is authoritative for the evidence and the user's role. The client
should display what the API returns and should not infer missing citations,
roles, or sensitivity labels.

## Frontend changes

The production bundle is baked into the application image. Rebuild the image to
see a frontend change in Docker:

```bash
docker compose build app
docker compose up -d app
```

`vite.config.ts` contains a minifier setting required by the current dependency
bundle; keep it when changing the build configuration.

For the backend and endpoint map, see [`api/README.md`](../api/README.md) and
[`docs/reference.md`](../docs/reference.md). For contribution conventions, see
[`CONTRIBUTING.md`](../CONTRIBUTING.md).
