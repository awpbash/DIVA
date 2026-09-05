# The single app container — React frontend built and served by FastAPI.
# Build context is the repo root; .dockerignore keeps storage/.venv out.
#
#   docker compose build app
#   docker compose up -d
#
# Stage 1 builds the SPA; stage 2 is the Python runtime that serves BOTH the
# API and the static bundle (api/main.py mounts web/dist with an SPA
# fallback). One container, one origin, no nginx, no CORS, no proxy route
# list to maintain.
#
# NOTE for the dev loop: compose source-mounts api/pipeline/configs/scripts, so
# a backend edit is live on restart, but the SPA is baked HERE. A frontend
# change needs `docker compose build app` before you will see it.

FROM node:20-alpine AS webbuild
WORKDIR /web
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm run build


FROM python:3.12-slim

LABEL org.opencontainers.image.title="Verbatim" \
      org.opencontainers.image.description="Schema-first document extraction with human verification and evidence-anchored retrieval." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/awpbash/verbatim"

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# rapidocr hard-requires opencv-python (NOT -headless), whose cv2 import dlopens
# libGL/libxcb/libgthread — absent from python:*-slim. Without these, any OCR in
# the container dies at import (ImportError: libxcb.so.1). apt is the boring,
# reliable fix; the layer sits before pip so both cache independently.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# --timeout/--retries so a slow mirror or a transient network blip doesn't
# fail the whole image build (heavy wheels like onnxruntime time out easily).
RUN pip install --timeout 120 --retries 5 -r requirements.txt

COPY pipeline/ pipeline/
COPY configs/ configs/
# Bake the RapidOCR ONNX models (~30MB) into the image. They otherwise
# download lazily into the ephemeral container filesystem on the first OCR
# run — repeated after every redeploy/cold start (rapidocr stores them under
# site-packages and honours no cache env var). _get_engine is the single
# source of truth for which models to fetch; placed before COPY api/ so
# api-only changes reuse this layer.
# FAIL-SOFT: CU is the default reader and the seeded demo needs no OCR, so a
# blocked model mirror (corporate SSL intercept, modelscope outage) must not
# kill the build. RapidOCR then lazy-downloads on first use, as it always did.
RUN python -c "from pipeline.extraction.rapidocr_ocr import _get_engine; _get_engine()" \
 || echo "WARN: RapidOCR model prefetch failed, image ships without baked models"
COPY api/ api/
# eval/ ships the review-record builder the admin extract chain reuses
# (eval.extraction.record) — one source of truth, no duplicated logic.
# scripts/ ships the ops levers (setup, accounts, rebuild_kb, reconcile_kb,
# build_km) so they run in-container too:
#   docker compose exec app python -m scripts.accounts list
COPY eval/ eval/
COPY scripts/ scripts/
# The Demo path (api/main.py's _load_demo_corpus) reads this at runtime.
# Missing here means every containerized deployment's Demo button silently
# does nothing, which is exactly what happened until this line existed.
COPY examples/ examples/
# Documentation ships in the image too, so a deployed container is the same
# checkout a cloned repo would be rather than a stripped-down runtime copy.
COPY docs/ docs/

# The built SPA — served by FastAPI (see api/main.py, SPA fallback).
COPY --from=webbuild /web/dist web/dist

# Drop root. This process parses PDFs and page images from wherever the
# operator got them, so it should not be uid 0. uid 1000 is deliberate: it is
# the first non-system uid on Linux and therefore the one that owns a
# bind-mounted ./storage created by an ordinary host user, which is what
# compose does. If your host user is not 1000, override it in compose with
#   user: "${UID}:${GID}"
# and make sure ./storage is writable by that user. Docker Desktop on Windows
# and macOS makes bind mounts writable regardless, so this only bites on Linux.
RUN useradd --create-home --uid 1000 app \
 && mkdir -p /app/storage \
 && chown -R app:app /app
USER app

# storage/ holds uploaded PDFs and every pipeline artifact. compose bind-mounts
# it; a cloud host should attach a persistent volume there. It is the one
# directory worth backing up. See docs/DEPLOYMENT.md.
#
# Deliberately NOT declared as a VOLUME: that would make every plain
# `docker run` mint an anonymous volume, which quietly accumulates orphaned
# copies of the corpus. Mount it explicitly instead.
EXPOSE 8000

# Liveness only — /readyz carries the dependency checks. An orchestrator
# must not kill the container because Cosmos blinked.
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=4).status == 200 else 1)"

# boot_seed: seed-on-empty shim for cloud volumes (no-op when STORAGE_SEED_URL
# is unset / storage already populated), then execs the same uvicorn line.
# Local compose overrides `command:` with uvicorn --reload, bypassing this.
CMD ["python", "-m", "api.boot_seed"]
