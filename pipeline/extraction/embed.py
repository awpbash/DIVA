"""
embed.py — vector-embed EvidenceSpans + Sections, write back to Cosmos.

Why embeddings sit on the same items (and not a separate vector store): one
store, one bill, one consistency story. Each embedded item carries an
``embedding`` field; retrieval runs either exact in-RAM cosine (dev/client
mode) or native Cosmos VectorDistance + DiskANN (COSMOS_VECTOR_MODE=native
on real Azure). See pipeline/store/client.py.

What gets embedded
------------------

* **EvidenceSpan** — the citation atom. Embedding text is the source
  ``text_span`` (the verbatim snippet) prefixed with its parent Section
  heading so that "consumption charge rate" matches even when the snippet
  reads as a bare number. This is the channel for fuzzy semantic recall.

* **Section** — the navigational anchor. Embedding text is
  ``"Section <num>: <title>"``. Lets the planner do section-level recall
  for vague questions like "what does Schedule 1B cover?" before drilling
  into facts.

CLI
---

* ``python -m pipeline.extraction.embed --ddl``      # ensure the container
* ``python -m pipeline.extraction.embed <doc_id>``   # embed all spans/sections for a doc
* ``python -m pipeline.extraction.embed --all``      # embed every doc in canonical/
"""
from __future__ import annotations

from . import token_meter

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

from ..config import Config, make_async_openai_embed
from ..store.client import CosmosStore, get_store


# text-embedding-3-large native dimension (also pinned in store/model.py —
# the container's vector policy needs it at creation time; switching embed
# model means recreating the container).
EMBED_DIMS = 3072


# ---------------------------------------------------------------------------
# Block noise filter — skip these from embedding
# ---------------------------------------------------------------------------
#
# Blocks of these kinds are page furniture. Figures were historically
# excluded, but OCR sometimes recovers substantial text from inside a
# figure (equipment labels, schematic callouts). When the figure contains
# real text it's the only place that text exists in the graph — keep
# the length thresholds below as the actual filter rather than blanket
# kind exclusion.
_BLOCK_SKIP_KINDS = frozenset({"footer", "header"})
# Below this length, embedding is noise — "(a)", "33", "Tel Fax" etc.
_BLOCK_MIN_CHARS = 20
# Below this word count, embedding is noise even when chars happen to be long
# (e.g. a long single token).
_BLOCK_MIN_WORDS = 4


def _is_embeddable_block(row: dict) -> bool:
    """True when a Block deserves an embedding. Skipped blocks still
    exist as nodes (citation lookup) — they just don't show up in
    vector recall."""
    kind = (row.get("kind") or "").strip().lower()
    if kind in _BLOCK_SKIP_KINDS:
        return False
    text = (row.get("text") or "").strip()
    if len(text) < _BLOCK_MIN_CHARS:
        return False
    if len(text.split()) < _BLOCK_MIN_WORDS:
        return False
    return True


def apply_ddl(cfg: Config) -> None:
    """No index DDL in the Cosmos world: the container's vector policy is set
    at creation (store.ensure) and keyword search is Python-side scoring.
    Kept as the ensure hook so callers read unchanged."""
    get_store(cfg).ensure()


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------


# Disk cache for embeddings, keyed by (model, text) content hash. Makes
# "wipe the store and rebuild from storage/" free and deterministic — the rebuild
# path re-embeds the exact same strings, so every lookup is a cache hit.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EMB_CACHE_ROOT = Path(os.environ.get("EMB_CACHE_DIR", str(_REPO_ROOT / "storage" / "emb_cache")))


def _emb_cache_path(model: str, text: str) -> Path:
    digest = hashlib.sha256(f"{model}\x00{text}".encode("utf-8")).hexdigest()
    # Two-level fan-out keeps any one directory small.
    return _EMB_CACHE_ROOT / digest[:2] / f"{digest}.json"


def _emb_cache_get(model: str, text: str) -> list[float] | None:
    p = _emb_cache_path(model, text)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _emb_cache_put(model: str, text: str, vector: list[float]) -> None:
    p = _emb_cache_path(model, text)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(vector), encoding="utf-8")


async def embed_texts(
    client: AsyncOpenAI, model: str, texts: list[str], *, batch: int = 96,
) -> list[list[float]]:
    """Embed in batches with a read/write disk cache. OpenAI accepts up to
    2048 inputs per call, but smaller batches keep memory pressure low and
    let retries be cheap."""
    out: list[list[float] | None] = [_emb_cache_get(model, t) for t in texts]
    misses = [i for i, v in enumerate(out) if v is None]
    for i in range(0, len(misses), batch):
        idxs = misses[i:i + batch]
        chunk = [texts[j] for j in idxs]
        resp = await client.embeddings.create(model=model, input=chunk)
        token_meter.record(resp.usage, stage="embed", model=model)
        for j, d in zip(idxs, resp.data):
            out[j] = d.embedding
            _emb_cache_put(model, texts[j], d.embedding)
    return out  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Per-doc work
# ---------------------------------------------------------------------------


def _fetch_evidence_rows(store: CosmosStore, doc_id: str) -> list[dict]:
    """Pull every evidence item in the doc with its parent section title.

    The parent section title is folded into the embedding text so a fact
    whose verbatim snippet is just a number still retrieves on a
    "consumption charge rate" query.
    """
    sections = {s["id"]: s for s in store.query(
        "SELECT c.id, c.section_num, c.title FROM c WHERE c.kind = 'section'",
        pk=doc_id)}
    rows = []
    for e in store.query(
            "SELECT c.id, c.text_span, c.section_id FROM c "
            "WHERE c.kind = 'evidence'", pk=doc_id):
        s = sections.get(e.get("section_id")) or {}
        rows.append({"evidence_id": e["id"], "text_span": e.get("text_span"),
                     "section_num": s.get("section_num"),
                     "section_title": s.get("title")})
    return rows


def _fetch_section_rows(store: CosmosStore, doc_id: str) -> list[dict]:
    return [
        {"section_id": r["id"], "section_num": r.get("section_num"),
         "title": r.get("title"), "kind": r.get("section_kind")}
        for r in store.query(
            "SELECT c.id, c.section_num, c.title, c.section_kind FROM c "
            "WHERE c.kind = 'section'", pk=doc_id)
    ]


def _fetch_block_rows(store: CosmosStore, doc_id: str) -> list[dict]:
    """Pull every block in the doc with its section_path. The path is
    folded into the embedding text so vector recall on 'supply temperature
    temperature' surfaces the right block even when the verbatim text
    doesn't repeat the section heading.

    For table blocks where merge.py recovered a grid, we render the grid
    as 'header: value' pairs in the embed text — preserves column
    meaning that the raw concatenated text loses.
    """
    return [
        {"block_id": r["id"], "kind": r.get("block_kind"), "text": r.get("text"),
         "section_path": r.get("section_path"), "grid_json": r.get("grid_json")}
        for r in store.query(
            "SELECT c.id, c.block_kind, c.text, c.section_path, c.grid_json "
            "FROM c WHERE c.kind = 'block'", pk=doc_id)
    ]


def _evidence_embed_text(row: dict) -> str:
    snippet = (row.get("text_span") or "").strip()
    section_num = (row.get("section_num") or "").strip()
    section_title = (row.get("section_title") or "").strip()
    if section_num or section_title:
        header = f"[Section {section_num}: {section_title}]".replace("Section : ", "")
        return f"{header} {snippet}".strip()
    return snippet


def _section_embed_text(row: dict) -> str:
    num = (row.get("section_num") or "").strip()
    title = (row.get("title") or "").strip()
    kind = (row.get("kind") or "section").strip()
    if num and title:
        return f"{kind.replace('_', ' ').title()} {num}: {title}"
    return title or num or kind


def _block_embed_text(row: dict) -> str:
    """Compose the text we feed the embedder for a :Block.

    Plain blocks: section breadcrumb prefix + verbatim text.
    Table blocks: section breadcrumb + 'header: value' rows (preserves
    column meaning).
    """
    section_path = list(row.get("section_path") or [])
    breadcrumb = " > ".join(s for s in section_path if s) if section_path else ""
    header = f"[{breadcrumb}]" if breadcrumb else ""

    kind = (row.get("kind") or "").strip().lower()
    if kind == "table":
        grid_json = row.get("grid_json")
        if grid_json:
            try:
                grid = json.loads(grid_json)
                headers = list(grid.get("headers") or [])
                rows = list(grid.get("rows") or [])
                lines = []
                for r in rows:
                    pairs = [
                        f"{(h or 'col').strip()}: {(str(c) or '').strip()}"
                        for h, c in zip(headers, r) if (h or c)
                    ]
                    if pairs:
                        lines.append("; ".join(pairs))
                body = "\n".join(lines) if lines else str(row.get("text") or "")
                return f"{header} {body}".strip() if header else body
            except (json.JSONDecodeError, ValueError):
                pass

    text = str(row.get("text") or "").strip()
    return f"{header} {text}".strip() if header else text


def _write_embeddings(store: CosmosStore, doc_id: str, rows: list[dict],
                      id_key: str) -> None:
    for r in rows:
        store.patch(r[id_key], doc_id, [
            {"op": "set", "path": "/embedding", "value": r["embedding"]},
            {"op": "set", "path": "/embed_text", "value": r["embed_text"]},
        ])


async def embed_doc(cfg: Config, doc_id: str) -> dict[str, int]:
    """Embed every evidence span + section + block for one doc. Returns counts."""
    client = make_async_openai_embed(cfg)
    counts: dict[str, int] = {}
    store = get_store(cfg)
    ev_rows = _fetch_evidence_rows(store, doc_id)
    sec_rows = _fetch_section_rows(store, doc_id)

    if ev_rows:
        ev_texts = [_evidence_embed_text(r) for r in ev_rows]
        vectors = await embed_texts(client, cfg.embed_model, ev_texts)
        payload = [
            {"evidence_id": r["evidence_id"], "embedding": v, "embed_text": t}
            for r, v, t in zip(ev_rows, vectors, ev_texts)
        ]
        _write_embeddings(store, doc_id, payload, "evidence_id")
        counts["EvidenceSpan"] = len(payload)

    if sec_rows:
        sec_texts = [_section_embed_text(r) for r in sec_rows]
        vectors = await embed_texts(client, cfg.embed_model, sec_texts)
        payload = [
            {"section_id": r["section_id"], "embedding": v, "embed_text": t}
            for r, v, t in zip(sec_rows, vectors, sec_texts)
        ]
        _write_embeddings(store, doc_id, payload, "section_id")
        counts["Section"] = len(payload)

    # Block embedding — full-coverage verbatim recall. Skips noise
    # (tiny / page-furniture blocks) per _is_embeddable_block.
    all_block_rows = _fetch_block_rows(store, doc_id)
    embeddable = [r for r in all_block_rows if _is_embeddable_block(r)]
    if embeddable:
        block_texts = [_block_embed_text(r) for r in embeddable]
        vectors = await embed_texts(client, cfg.embed_model, block_texts)
        payload = [
            {"block_id": r["block_id"], "embedding": v, "embed_text": t}
            for r, v, t in zip(embeddable, vectors, block_texts)
        ]
        _write_embeddings(store, doc_id, payload, "block_id")
        counts["Block"] = len(payload)
        counts["Block_skipped"] = len(all_block_rows) - len(embeddable)

    return counts


def _list_doc_ids(cfg: Config) -> list[str]:
    canonical_dir = cfg.storage_root / "canonical"
    if not canonical_dir.exists():
        return []
    return sorted(p.stem for p in canonical_dir.glob("*.json"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Embed EvidenceSpans + Sections into Cosmos.")
    ap.add_argument("doc_id", nargs="?", help="Doc id to embed.")
    ap.add_argument("--ddl", action="store_true",
                    help="Ensure the database + container (idempotent).")
    ap.add_argument("--all", action="store_true",
                    help="Embed every doc in storage/canonical/.")
    args = ap.parse_args()

    cfg = Config.load()

    if args.ddl:
        apply_ddl(cfg)
        print("[DDL]   database + container ensured")

    doc_ids: list[str] = []
    if args.all:
        doc_ids = _list_doc_ids(cfg)
    elif args.doc_id:
        doc_ids = [args.doc_id]

    for doc_id in doc_ids:
        counts = asyncio.run(embed_doc(cfg, doc_id))
        total = sum(counts.values())
        print(f"[EMBED] doc_id={doc_id}  total={total}  {counts}")

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
