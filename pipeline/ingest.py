"""
ingest.py — THE one-command ingestion driver.

    python -m pipeline.ingest path/to/contract.pdf
    python -m pipeline.ingest <doc_id>              # already in storage/raw/
    python -m pipeline.ingest --all                 # every pdf in storage/raw/

Runs every stage of the pipeline in order for a document. Two modes
(env EXTRACTION_MODE > CLI --mode > configs/pipeline.yaml extraction_mode):

    legacy        render -> READ -> merge -> table_repair
                  -> understand -> validate -> repair -> load (+ derive + hubs)
                  -> ground -> docmeta -> timeline -> embed
    schema_first  render -> READ -> merge -> table_repair
                  -> canonical_lite (fact-less) -> load (structure only)
                  -> docmeta -> embed
                  (the ops-view extraction + review/KM layer carry the
                  knowledge; understand/validate/ground/timeline are skipped)

READ is picked by env READER (both modes, both writers land in pages_md/):

    rapidocr (default)  rapidocr -> correct_classify   local OCR + LLM vision fix
    cu                  cu_read                        Azure Content Understanding

Every stage is idempotent (content-addressed caches / MERGE writes), so
re-running an already-ingested doc is cheap no-ops all the way down — this
driver is safe to point at the same file twice. DDL (constraints + vector
indexes) is applied on every run; all statements are IF NOT EXISTS.

A new contract needs exactly this one command — no manual stage chaining.
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from .config import Config
from .storage import Paths, doc_id_for
from .extraction import (
    canonical_lite,
    correct_classify,
    cu_read,
    embed,
    merge,
    rapidocr_ocr,
    render,
    table_repair,
)
from .extraction.pack import load as load_pack
from .ontology import DEFAULT_DOCTYPE
from .kb import docmeta as docmeta_mod
from .kb import load as load_mod


def _intake(cfg: Config, arg: str) -> str:
    """Resolve a CLI arg to a doc_id. A path to a PDF gets copied into
    storage/raw/ under its content-hash id; a bare id is used as-is.

    Also writes a ``<doc_id>.meta.json`` sidecar capturing the original
    filename, source path, and contract-family group (parent folder), so the
    loader can stamp human document metadata for a one-off ingest without a
    separate ``data/`` folder scan."""
    paths = Paths(cfg)
    p = Path(arg)
    if p.suffix.lower() == ".pdf":
        if not p.exists():
            raise FileNotFoundError(p)
        doc_id = doc_id_for(p)
        dst = paths.raw_pdf(doc_id)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, dst)
            print(f"[INTAKE] {p.name} -> {dst.name} (doc_id={doc_id})")
        else:
            print(f"[INTAKE] {p.name} already in storage (doc_id={doc_id})")
        # Capture human metadata once (don't overwrite a richer earlier sidecar).
        meta_path = paths.raw_meta_json(doc_id)
        if not meta_path.exists():
            try:
                src = p.resolve()
                rel = str(src).replace("\\", "/")
                try:
                    rel = str(src.relative_to(Path.cwd())).replace("\\", "/")
                except ValueError:
                    pass
                import json as _json
                meta_path.write_text(_json.dumps({
                    "title": p.stem,
                    "source_path": rel,
                    "group": src.parent.name or None,
                }, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return doc_id
    if not paths.raw_pdf(arg).exists():
        raise FileNotFoundError(f"no storage/raw/{arg}.pdf — pass a pdf path to intake it")
    return arg


def ingest_doc(cfg: Config, doc_id: str, *, force: bool = False,
               mode: str | None = None) -> None:
    """Run the full stage chain for one doc. Stages print their own
    cache-hit/miss lines; this driver prints stage boundaries + timing.

    Sync on purpose: the stages disagree about async (understand.run_async
    returns a coroutine; correct_classify wraps asyncio.run), so each async
    stage gets its own event loop here.
    """

    from .extraction import token_meter
    token_meter.reset()

    def stage(name: str, fn, *args, **kwargs):
        t0 = time.monotonic()
        print(f"--- {name} ---", flush=True)
        out = fn(*args, **kwargs)
        print(f"--- {name} done in {time.monotonic()-t0:.1f}s ---", flush=True)
        return out

    pack = load_pack(DEFAULT_DOCTYPE)

    # Reading stage (filesystem artifacts).
    # The READER switch picks who fills pages_md/: the local RapidOCR + LLM
    # correct/classify pair (default, free, offline) or Azure Content
    # Understanding (one paid call per doc, cached forever in cu_raw/).
    print(f"[READ]  reader = {cfg.reader}", flush=True)
    stage("render", render.run, cfg, doc_id, force=force)
    if cfg.reader in ("cu", "content_understanding"):
        stage("cu_read", cu_read.process_doc, cfg, doc_id, force=force)
    else:
        stage("rapidocr", rapidocr_ocr.process_doc, cfg, doc_id, force=force)
        stage("correct_classify",
              lambda: asyncio.run(correct_classify.run_async(
                  cfg, doc_id, force=force, concurrency=cfg.vision_concurrency)))
    stage("merge", merge.run, cfg, doc_id, force=force)
    stage("table_repair", table_repair.run, cfg, doc_id)

    # The canonical artifact carries structure only. Knowledge comes from the
    # ops-view extraction (pipeline.kb.field_llm) plus the review/KM layer.
    stage("canonical_lite", canonical_lite.run, cfg, doc_id)
    load_mod.apply_ddl(cfg, pack)
    embed.apply_ddl(cfg)
    counts = stage("load", load_mod.load_doc, cfg, doc_id)
    print(f"        nodes: {sum(counts.values())}")
    print(f"        {stage('docmeta', docmeta_mod.backfill, cfg)}")
    emb_counts = stage("embed",
                       lambda: asyncio.run(embed.embed_doc(cfg, doc_id)))
    print(f"        {emb_counts}")

    # Per-stage LLM token accounting for this run (meter reset at the top).
    import json as _json
    paths = Paths(cfg)
    snap = token_meter.snapshot()
    # Per-run detail.
    paths.ensure_parent(paths.token_usage_json(doc_id)).write_text(
        _json.dumps({"doc_id": doc_id, **snap}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    # Consolidated ledger — one tokens.json keyed by doc_id, merged across
    # ingests so the whole corpus's per-stage token cost lives in one file.
    tj = paths.tokens_json()
    try:
        ledger = _json.loads(tj.read_text(encoding="utf-8")) if tj.exists() else {}
    except (_json.JSONDecodeError, OSError):
        ledger = {}
    # A fully-cached (0-token) re-ingest must not wipe a recorded build cost.
    prev_total = ledger.get(doc_id, {}).get("total", {}).get("total_tokens", 0)
    if snap["total"]["total_tokens"] > 0 or prev_total == 0:
        ledger[doc_id] = snap
    paths.ensure_parent(tj).write_text(
        _json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    print("--- token usage (this run) ---", flush=True)
    print(token_meter.report(), flush=True)


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Full-pipeline ingestion for one or more PDFs.")
    ap.add_argument("target", nargs="?", help="PDF path or doc_id.")
    ap.add_argument("--all", action="store_true", help="Ingest every pdf in storage/raw/.")
    ap.add_argument("--force", action="store_true",
                    help="Bypass per-stage caches (full re-extraction; costs LLM calls).")
    ap.add_argument("--mode", choices=["legacy", "schema_first"], default=None,
                    help="Extraction mode override (env EXTRACTION_MODE beats this; "
                         "default comes from configs/pipeline.yaml).")
    args = ap.parse_args()

    cfg = Config.load()

    if args.all:
        doc_ids = sorted(p.stem for p in (cfg.storage_root / "raw").glob("*.pdf"))
    elif args.target:
        doc_ids = [_intake(cfg, args.target)]
    else:
        ap.error("pass a pdf path / doc_id, or --all")
        return 2

    for doc_id in doc_ids:
        print(f"\n===== INGEST {doc_id} =====")
        ingest_doc(cfg, doc_id, force=args.force, mode=args.mode)
        print(f"===== {doc_id} COMPLETE =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
