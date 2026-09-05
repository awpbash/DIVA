"""FastAPI app entrypoint.

::

    .venv/Scripts/python -m uvicorn api.main:app --reload --port 8000

Endpoints (see ``routes/`` for full schemas)::

    GET  /healthz                 liveness (process up)
    GET  /readyz                  readiness (Cosmos + storage reachable)
    GET  /documents               list ingested docs
    GET  /pdf/{doc_id}            stream raw PDF (Content-Type: application/pdf)
    GET  /evidence/{evidence_id}  page_no + bbox/rects + snippet for highlight
    POST /chat                    SSE stream — multi-turn Q&A with citations
    GET  /graph/subgraph          per-answer subgraph (cited nodes + 1-hop)
    GET  /graph/overview          full graph for the Explore tab

Production knobs (all env vars, all optional)::

    CHAT_API_KEY            require X-API-Key (or ?api_key=) on every route
    CHAT_CORS_ORIGINS       comma-separated exact origins
    CHAT_CORS_ORIGIN_REGEX  replaces the dev LAN-origin regex
    CHAT_DISABLED_TOOLS     comma-separated tool kill-switch
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import secrets
import sys
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from pipeline.store.aio import STORE_UNAVAILABLE_ERRORS

from . import __version__, appdb, deps, review_votes
from .routes import (
    admin, auth, chat, docs, evidence, feedback, graph, km, ontology, policy,
    registry, review, setup, threads,
)
from .settings import get_settings


# Console encoding must never be able to kill a response stream: on
# Windows a cp1252 console makes any print/log containing → or ✓ raise
# UnicodeEncodeError mid-SSE. Force UTF-8 with replacement instead of
# depending on the PYTHONUTF8 env var being set.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


# Wire our rag.* loggers into stdout. uvicorn's default config only sets
# up its own loggers; without this, `logging.getLogger("rag.chat")` calls
# go nowhere and the agent loop is invisible.
#
# LOG_FORMAT=json switches to one-JSON-object-per-line (what Container Apps /
# Log Analytics parse into queryable columns); the default stays human-
# readable for local dev.


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json as _json
        out = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return _json.dumps(out, ensure_ascii=False)


import os as _os

_log_handler = logging.StreamHandler(stream=sys.stdout)
if (_os.environ.get("LOG_FORMAT") or "").lower() == "json":
    _log_handler.setFormatter(_JsonFormatter())
else:
    _log_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    ))
for _name in ("rag", "rag.chat", "rag.agent", "rag.tools", "api",
              "api.jobs", "store"):
    _l = logging.getLogger(_name)
    _l.setLevel(logging.INFO)
    if not _l.handlers:
        _l.addHandler(_log_handler)
    _l.propagate = False
# The Azure SDK logs every HTTP request at INFO — keep it to warnings.
logging.getLogger("azure").setLevel(logging.WARNING)

_access_log = logging.getLogger("api.access")
_error_log = logging.getLogger("api.error")


def _log_security_posture() -> None:
    """Say out loud, once per boot, what is and is not protecting this instance.

    Sign-in is passwordless: an email address alone is enough. That is a
    reasonable posture on a laptop and a bad one on a public address, and the
    difference is invisible from inside the process. So state it in the log
    rather than let an operator infer it. See SECURITY.md.
    """
    log = logging.getLogger("api")
    log.warning("sign-in is PASSWORDLESS: anyone who can reach this instance "
                "and knows an account's email address can sign in as them")
    if _settings.api_key:
        log.info("shared-secret gate is ON (CHAT_API_KEY set)")
    else:
        log.warning("no shared-secret gate (CHAT_API_KEY unset). Keep this "
                    "instance on a private network or behind a proxy that "
                    "authenticates. See docs/DEPLOYMENT.md")


def _load_demo_corpus() -> None:
    """One-shot: ingest examples/corpus/*.pdf through the real upload path,
    with the intake declarations from examples/manifest.py (both amendments
    declare themselves as amending the base agreement — see
    examples/README.md for why the corpus is shaped this way). Triggered
    once by the setup wizard's Demo path
    (api/routes/setup.py:choose_demo_domain) and consumed (cleared) by the
    lifespan handler below before this runs, so a later restart never
    re-ingests it."""
    log = logging.getLogger("api")
    try:
        from api.routes.admin import ingest_local_pdf
        from examples.manifest import CORPUS, CORPUS_DIR
        corpus_root = Path(__file__).resolve().parents[1] / "examples" / CORPUS_DIR
        paths = {item["file"]: corpus_root / item["file"] for item in CORPUS}
        missing = [name for name, p in paths.items() if not p.exists()]
        if missing:
            log.warning("demo corpus files missing under %s: %s — skipping",
                       corpus_root, missing)
            return
        admins = [u for u in appdb.list_users() if u["role"] == "admin"]
        admin_email = admins[0]["email"] if admins else appdb.default_bootstrap_email()
        doc_ids: dict[str, str] = {}
        for item in CORPUS:
            parent = item.get("amends")
            doc_ids[item["file"]] = ingest_local_pdf(
                paths[item["file"]], admin_email=admin_email,
                document_type=item["document_type"], document_date=item["document_date"],
                relation=item.get("relation"),
                parent_doc_id=doc_ids.get(parent) if parent else None,
            )
        log.info("demo corpus loaded: %s", ", ".join(paths))
    except Exception:  # noqa: BLE001 — best-effort, must never block boot
        log.warning("demo corpus bootstrap failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log_security_posture()
    # Catches a total, silent event-loop freeze (every route stops, including
    # /healthz, CPU idle) that ptrace-based tools (py-spy, gdb) can't diagnose
    # here because the container's seccomp profile blocks attach. See
    # api/diagnostics.py for the detection method.
    from . import diagnostics
    diagnostics.start(asyncio.get_running_loop())
    # A fresh process has made zero calls so far. Also keeps this in-memory
    # state from leaking between tests that each spin up their own
    # TestClient(app) in the same pytest process.
    from . import ratelimit
    ratelimit.reset()
    # The app database first, and before anything that talks to a network. It
    # is a local sqlite file holding the accounts, so a fresh instance must end
    # up with its first admin even when every remote dependency is down. An
    # instance you cannot sign into is indistinguishable from a broken one.
    appdb.init()
    await deps.init()
    # File seam: on Azure, restore the storage/ working cache from Blob
    # before anything reads it (dev: no-op). Runs on a thread so a large
    # first pull can't starve the event loop; requests meanwhile 503 via
    # /readyz until the store answers.
    import threading as _threading

    from pipeline import filestore
    from .settings import get_config as _get_config
    if filestore.enabled():
        _threading.Thread(target=filestore.pull,
                          args=(_get_config(),), daemon=True).start()

    # Create the database and container if they are absent. Idempotent, free,
    # and the difference between `docker compose up -d` working and an instance
    # that serves a login screen which never becomes ready, because the only
    # thing that ever created them was a setup command the operator had not run
    # yet. On a thread: a cold emulator takes up to a minute and the app must
    # answer /healthz and show a login screen throughout.
    def _ensure_store() -> None:
        from pipeline.config import Config
        from pipeline.store.client import CosmosStore
        log = logging.getLogger("api")
        try:
            store = CosmosStore(Config.load())
            store.wait_ready(timeout=180)
            store.ensure()
            log.info("knowledge store ready")
        except Exception as exc:  # noqa: BLE001 — /readyz reports the truth
            log.warning("could not prepare the knowledge store (%s). The app "
                        "will serve but stay not-ready until it is reachable. "
                        "Check COSMOS_URI, and that the database container is "
                        "up: docker compose up -d cosmos", exc)

    _threading.Thread(target=_ensure_store, daemon=True).start()
    try:
        # Jobs that were 'running' when the last process died: flag them so
        # the dashboard shows a clear retry prompt instead of a stuck spinner.
        n = appdb.jobs_mark_interrupted()
        if n:
            logging.getLogger("api.jobs").warning(
                "marked %d orphaned job(s) interrupted after restart", n)
    except Exception:  # noqa: BLE001 — a locked app.db must not block boot
        pass
    try:
        # One-time: pre-vote single-verifier overlays become one approve vote each
        # (idempotent no-op afterwards). Best-effort — a locked file must not block boot.
        review_votes.migrate_legacy_overlays()
    except Exception:  # noqa: BLE001
        logging.getLogger("api").warning("legacy overlay migration failed", exc_info=True)
    try:
        # Documents registry table = a projection of the intake sidecars;
        # rebuild it at boot so declarations made before the table existed
        # (or a lost app.db) show up. Idempotent, ~1ms per document.
        from pipeline.kb import registry as _registry
        _registry.sync_documents_from_sidecars(_get_config())
        # Folders are the same idea for families: every sidecar group gets a
        # nameable folder row (never overwrites an admin-chosen name).
        _registry.sync_folders_from_groups(_get_config())
    except Exception:  # noqa: BLE001
        logging.getLogger("api").warning("document registry sync failed", exc_info=True)
    try:
        # Setup-wizard demo path (see api/routes/setup.py:choose_demo_domain):
        # cleared BEFORE running so a crash mid-ingest can't loop the seed on
        # every subsequent restart. Runs on a thread — ingestion makes real
        # model calls and must not block /healthz from answering.
        if appdb.get_setting("setup_load_demo_corpus") == "1":
            appdb.set_setting("setup_load_demo_corpus", "0")
            _threading.Thread(target=_load_demo_corpus, daemon=True).start()
    except Exception:  # noqa: BLE001
        logging.getLogger("api").warning("demo corpus bootstrap check failed", exc_info=True)
    try:
        yield
    finally:
        await deps.shutdown()


_settings = get_settings()

app = FastAPI(
    title=_settings.app_name,
    version=__version__,
    description="Schema-first extraction, human verification, and evidence-anchored "
                "retrieval over a document corpus.",
    lifespan=lifespan,
)


# --- middleware ------------------------------------------------------------
# Registration order matters: CORSMiddleware is added LAST so it wraps
# everything — auth 401s and error 500s still carry CORS headers, instead
# of surfacing in the browser as opaque CORS failures.


@app.middleware("http")
async def _access_logging(request: Request, call_next):
    t0 = time.monotonic()
    response = await call_next(request)
    ms = (time.monotonic() - t0) * 1000
    _access_log.info(
        "%s %s -> %d (%.0fms)",
        request.method, request.url.path, response.status_code, ms,
    )
    return response


# Paths a request may reach even when setup has not finished. Everything
# else that matches a KNOWN API prefix below is gated; anything that matches
# NEITHER list (any static asset / SPA route the catch-all at the bottom of
# this file serves) is left alone here, so the wizard's own page shell, JS
# and CSS always load — the React app decides client-side whether to render
# the wizard or the real app, based on GET /setup/status.
_SETUP_EXEMPT_PREFIXES = ("/setup", "/healthz", "/readyz", "/branding",
                         "/openapi.json", "/docs", "/redoc")
# Kept as an explicit allow-list (rather than routing introspection) so it's
# obvious at a glance which surfaces are reachable pre-setup. Add a new
# router's prefix here when you register one below.
_GATED_PREFIXES = ("/documents", "/pdf", "/evidence", "/restricted-regions",
                   "/chat", "/graph", "/policy", "/review", "/ontology",
                   "/km", "/auth", "/threads", "/admin", "/registry",
                   "/feedback")


def _path_under(path: str, prefixes: tuple[str, ...]) -> bool:
    """True if `path` IS one of `prefixes` or a sub-path of one — a plain
    `str.startswith` would also match an unrelated route that merely shares a
    text prefix (`/adminfoo` "starting with" `/admin`)."""
    return any(path == p or path.startswith(p + "/") for p in prefixes)


@app.middleware("http")
async def _setup_gate(request: Request, call_next):
    path = request.url.path
    if (request.method == "OPTIONS"
            or _path_under(path, _SETUP_EXEMPT_PREFIXES)
            or not _path_under(path, _GATED_PREFIXES)):
        return await call_next(request)
    # to_thread: this runs on almost every request (everything under
    # _GATED_PREFIXES), and unlike a sync route handler or a sync FastAPI
    # dependency, Starlette middleware gets no automatic threadpool offload
    # for a plain blocking call. appdb.is_setup_complete() does a synchronous
    # sqlite read, so calling it directly here ties up the SHARED event loop
    # for as long as that read takes, on every gated request, which starves
    # every OTHER pending request (including /healthz, exempt from the gate
    # itself but not from the loop being busy) until it returns. This is very
    # likely what an earlier, harder-to-pin-down freeze on this instance
    # actually was.
    if await asyncio.to_thread(appdb.is_setup_complete):
        return await call_next(request)
    return JSONResponse(
        {"detail": "This instance is not set up yet. Open it in a browser "
                   "to finish setup.", "setup_required": True},
        status_code=503)


@app.middleware("http")
async def _api_key_gate(request: Request, call_next):
    key = _settings.api_key
    if (key
            and request.method != "OPTIONS"          # CORS preflight
            and request.url.path != "/healthz"):     # monitoring
        supplied = (request.headers.get("x-api-key")
                    or request.query_params.get("api_key") or "")
        if not secrets.compare_digest(supplied, key):
            return JSONResponse({"detail": "invalid or missing API key"},
                                status_code=401)
    return await call_next(request)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    # Never leak stack traces to clients; full trace goes to the server log.
    _error_log.error("unhandled on %s %s: %s\n%s",
                     request.method, request.url.path, exc,
                     traceback.format_exc())
    return JSONResponse({"detail": "internal server error"}, status_code=500)


@app.exception_handler(STORE_UNAVAILABLE_ERRORS[0])
@app.exception_handler(STORE_UNAVAILABLE_ERRORS[1])
async def _store_warming(request: Request, exc: Exception) -> JSONResponse:
    """Knowledge store unreachable/booting → a retryable 503 "warming"
    signal, NOT a 500. The `warming` flag lets the frontend show "waking up,
    hang on" and retry instead of looking crashed."""
    _error_log.warning("store not ready on %s %s: %s",
                       request.method, request.url.path, exc)
    return JSONResponse(
        {"detail": "The knowledge base is starting up. Retry in a few seconds.",
         "warming": True},
        status_code=503, headers={"Retry-After": "5"})


app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_settings.cors_origins),
    allow_origin_regex=_settings.cors_origin_regex,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/branding")
async def branding() -> JSONResponse:
    """What this instance calls itself, plus which domain it is configured for.
    Public and unauthenticated: the login screen needs it before anyone has a
    session. Fetched once at boot so renaming an instance is an env var and a
    restart, never a frontend rebuild."""
    from pipeline import ontology
    try:
        domain = ontology.DEFAULT_DOCTYPE
    except Exception:  # noqa: BLE001 — an unconfigured instance still renders
        domain = ""
    try:
        # Only ever set on an instance nobody has signed into yet. See
        # appdb.first_run_email: it is how the operator learns the address
        # their own install created, and it disappears after the first login.
        # to_thread: unauthenticated and public, so this is one of the more
        # frequently hit routes (every login screen load), and a sync sqlite
        # call here runs directly on the shared event loop otherwise.
        hint = await asyncio.to_thread(appdb.first_run_email)
    except Exception:  # noqa: BLE001 — a locked app.db must not blank the page
        hint = None
    # The upload form's document-type menu. Domain vocabulary, so it is served
    # rather than written into the frontend, which used to offer one domain's
    # words to every deployment.
    try:
        from pipeline.kb.intake import doc_types
        types = list(doc_types())
    except Exception:  # noqa: BLE001 — an unconfigured instance still renders
        types = []
    return JSONResponse({"app_name": _settings.app_name,
                         "tagline": _settings.app_tagline,
                         "domain": domain,
                         "version": __version__,
                         "sign_in_hint": hint,
                         "document_types": types,
                         "document_noun": _settings.document_noun,
                         "document_noun_plural": _settings.document_noun_plural})


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness only: the process is up and serving. Dependency health lives
    in /readyz — an orchestrator must not kill the container just because a
    downstream service blinked."""
    return JSONResponse({"ok": True})


@app.get("/readyz")
async def readyz() -> JSONResponse:
    """Readiness: the dependencies this app cannot serve without. Pings
    Cosmos (one cheap metadata read) and checks the storage root is
    reachable, so orchestrator probes and load balancers see real
    readiness with a per-dependency breakdown."""
    from .settings import get_config
    checks: dict[str, str] = {}
    ok = True
    try:
        await deps.get_store().verify_connectivity()
        checks["cosmos"] = "up"
    except Exception as exc:  # noqa: BLE001 — any failure means not ready
        _error_log.warning("readyz: cosmos unreachable: %s", exc)
        checks["cosmos"] = "down"
        ok = False
    # Reported, never fatal. Without a key the instance still serves everything
    # it has already read, so killing the container over it would be wrong. The
    # operator needs to SEE it, which is what this line is for.
    try:
        checks["models"] = ("configured" if get_config().openai_api_key
                            else "no api key")
    except Exception:  # noqa: BLE001
        checks["models"] = "unknown"
    try:
        checks["storage"] = "up" if get_config().storage_root.exists() else "down"
        ok = ok and checks["storage"] == "up"
    except Exception:  # noqa: BLE001
        checks["storage"] = "down"
        ok = False
    return JSONResponse({"ok": ok, **checks}, status_code=200 if ok else 503)


app.include_router(setup.router)
app.include_router(docs.router)
app.include_router(evidence.router)
app.include_router(graph.router)
app.include_router(chat.router)
app.include_router(policy.router)
app.include_router(review.router)
app.include_router(ontology.router)
app.include_router(km.router)
app.include_router(auth.router)
app.include_router(threads.router)
app.include_router(admin.router)
app.include_router(registry.router)
app.include_router(feedback.router)


# --- the SPA (single-container mode) ----------------------------------------
# FastAPI serves the built React bundle directly: one container, one origin,
# no nginx, no proxy route list to maintain. API routes are registered ABOVE,
# so they always win; this catch-all only sees non-API paths. In dev (Vite on
# :5173, no web/dist) the block simply doesn't mount.

_WEB_DIST = Path(__file__).resolve().parents[1] / "web" / "dist"
if _WEB_DIST.is_dir():
    # PDF.js ships an ES-module worker (pdf.worker.min-*.mjs); browsers
    # refuse to start a module worker served as octet-stream, which
    # surfaces as "Failed to load PDF." Map .mjs explicitly.
    mimetypes.add_type("application/javascript", ".mjs")
    _INDEX = _WEB_DIST / "index.html"

    @app.get("/{spa_path:path}", include_in_schema=False)
    async def _spa(spa_path: str) -> FileResponse:
        # A `..` segment is never a legitimate SPA route or asset. Hard 404,
        # never the app-shell fallback — a traversal probe must not get a 200.
        if ".." in spa_path.split("/"):
            raise HTTPException(404, "not found")
        candidate = (_WEB_DIST / spa_path).resolve() if spa_path else _INDEX
        try:
            candidate.relative_to(_WEB_DIST)      # path-traversal guard
        except ValueError:
            raise HTTPException(404, "not found") from None
        if candidate.is_file() and candidate != _INDEX:
            return FileResponse(candidate)
        # The app shell: client-side routes (/, /review, /knowledge, ...)
        # fall through to it, and a direct /index.html request is forced
        # through here too (the is_file() check above excludes it). It names
        # the CURRENT build's content-hashed asset filenames, so unlike those
        # assets it must never be cached — a browser holding a stale copy
        # after a rebuild keeps quietly running old frontend code with no
        # error, which is exactly what let a prior wizard test look "broken"
        # when the real cause was cached HTML pointing at a bundle the
        # server no longer serves. FileResponse sets Last-Modified/ETag on
        # its own; only Cache-Control needs overriding.
        return FileResponse(_INDEX, headers={"Cache-Control": "no-store"})
