"""kb/load.py — the structure loader.

Writes a document's skeleton into the store: Document, Section, Block. That is
all it writes.

Knowledge does NOT live here. It lives in the ops-view fields that
``pipeline.kb.field_llm`` extracts against the declared schema, and in the KM
layer built over the human-verified versions of those fields. This loader gives
that layer a document to hang off and keeps the raw text searchable for vector
and keyword retrieval.

Until 2026-08-30 this module also loaded an open-vocabulary fact tier: facts
partitioned by a quarantine gate, mention provenance, derived fact-to-fact
edges, a living parameter vocabulary and a concept layer. That tier offered a
second way to do the same job and was retired with the rest of the open-vocab
pipeline.

Invariants kept:
  * idempotent upsert-on-key — re-running a doc is safe
  * a re-load never wipes an embedding

Cosmos has no DDL: the container (with its vector policy) is ensured once by
the store (``apply_ddl`` below is that ensure, kept under the old name so the
rebuild orchestration reads unchanged).

CLI:
  python -m pipeline.kb.load --ddl        ensure database + container
  python -m pipeline.kb.load --nuke       drop + recreate container
  python -m pipeline.kb.load <doc_id>     load one doc
"""
from __future__ import annotations

import argparse
import json

from dotenv import load_dotenv

from ..config import Config
from ..storage import Paths
from ..extraction.pack import Pack, load as load_pack
from ..ontology import DEFAULT_DOCTYPE
from ..extraction.sections import extract_sections
from .writers import (
    _store,
    _write_blocks,
    _write_document,
    _write_sections,
    existing_embeddings,
    nuke,
)


def apply_ddl(cfg: Config, pack: Pack) -> int:
    """Ensure database + container (with the vector policy where the backend
    accepts it). The pack's uniqueness constraints are structural in Cosmos:
    the natural key IS the item id. Kept under the old name so rebuild
    orchestration reads unchanged. Returns 1 (one container ensured)."""
    _store(cfg).ensure()
    return 1


def load_doc(cfg: Config, doc_id: str) -> dict[str, int]:
    """Load one document's structure tier. Idempotent."""
    paths = Paths(cfg)
    pack = load_pack(DEFAULT_DOCTYPE)

    canon = json.loads(paths.canonical_json(doc_id).read_text(encoding="utf-8"))
    doc = json.loads(paths.doc_json(doc_id).read_text(encoding="utf-8"))
    geom_path = paths.doc_geometry_json(doc_id)
    geometry = (json.loads(geom_path.read_text(encoding="utf-8"))
                if geom_path.exists() else {})

    sections = extract_sections(doc, doc_id=doc_id, geometry=geometry)

    # Human document metadata captured at intake (optional sidecar). Lets a
    # one-off ingest stamp title/source_path/group without a folder scan.
    meta_path = paths.raw_meta_json(doc_id)
    meta = (json.loads(meta_path.read_text(encoding="utf-8"))
            if meta_path.exists() else {})

    store = _store(cfg)
    store.ensure()
    # Fetch the doc's existing vectors once and re-attach them, so a re-load
    # never costs a re-embed.
    keep = existing_embeddings(store, doc_id)
    _write_document(
        store, doc_id=doc_id,
        doctype=canon.get("doctype") or pack.doctype,
        analyzer_id=canon.get("analyzer_id") or "_universal",
        n_pages=int(doc.get("n_pages") or 0), n_facts=0,
        title=meta.get("title"), source_path=meta.get("source_path"),
        group=meta.get("group"),
        extraction_mode="schema_first",
    )
    _write_sections(store, doc_id=doc_id, sections=sections, keep_embeddings=keep)
    n_blocks = _write_blocks(
        store, doc_id=doc_id, doc=doc, geometry=geometry, sections=sections,
        keep_embeddings=keep,
    )

    return {
        "Document": 1,
        "Agreement": 1,
        "Section": len(sections),
        "Block": n_blocks,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Pack-driven structure loader (KB rebuild).")
    ap.add_argument("doc_id", nargs="?")
    ap.add_argument("--ddl", action="store_true", help="Ensure database + container.")
    ap.add_argument("--nuke", action="store_true", help="Drop + recreate the container.")
    args = ap.parse_args()
    cfg = Config.load()

    if args.nuke:
        print(f"[NUKE]  deleted {nuke(cfg)} nodes")
    if args.ddl:
        n = apply_ddl(cfg, load_pack(DEFAULT_DOCTYPE))
        print(f"[DDL]   applied {n} statements")
    if args.doc_id:
        counts = load_doc(cfg, args.doc_id)
        print(f"[LOAD]  doc_id={args.doc_id}  total={sum(counts.values())}")
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"          {k:26s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
