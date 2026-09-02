"""kb/docmeta.py — backfill human document metadata onto Document nodes.

The loader only stamps a content-hash ``doc_id`` and ``doctype`` — the
original filename, its source path, and which contract FAMILY it belongs to
are lost at intake (PDFs are copied to ``storage/raw/<hash>.pdf``). But those
are exactly what the retriever needs to (a) name + link documents in answers
and (b) scope a site-named question to the right contract family.

This module rebuilds that metadata deterministically by scanning the source
PDF folders: it hashes every ``*.pdf`` (same ``doc_id_for`` the pipeline uses)
and writes onto the matching ``Document`` node:

  * ``title``       — the original filename, which is usually descriptive
                      ("Northwind Logistics - Master Services Agreement")
  * ``source_path`` — repo-relative path of the source PDF
  * ``group``       — the immediate parent folder when the PDF lives in a
                      per-contract subfolder (the contract FAMILY, e.g.
                      "Dummy Contract 3"); ``None`` for standalone PDFs sitting
                      directly in a scan root.

Idempotent (MERGE-free SET on matched nodes). Re-runnable; safe to call from
``rebuild_kb`` after every load. A doc with no source PDF under the scan roots
is simply skipped (its title stays the doctype fallback).

CLI:
    python -m pipeline.kb.docmeta                 # scan default roots (data/)
    python -m pipeline.kb.docmeta path/to/dir ... # scan explicit roots
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from ..config import Config
from ..storage import doc_id_for
from .writers import _store

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ROOTS = [_REPO_ROOT / "data"]


def scan_sources(roots: list[Path]) -> dict[str, dict]:
    """Map doc_id -> {title, source_path, group} for every PDF under ``roots``.

    Pure (filesystem only). ``group`` is the immediate parent folder name when
    the PDF is nested in a subfolder of the scan root (= the contract family);
    a PDF directly in a root has no group. On a hash collision (same bytes in
    two places) the first path wins — deterministic via sorted() iteration.
    """
    out: dict[str, dict] = {}
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for pdf in sorted(root.rglob("*.pdf")):
            doc_id = doc_id_for(pdf)
            if doc_id in out:
                continue
            parent = pdf.parent
            group = parent.name if parent.resolve() != root.resolve() else None
            try:
                source_path = str(pdf.resolve().relative_to(_REPO_ROOT)).replace("\\", "/")
            except ValueError:
                source_path = str(pdf.resolve()).replace("\\", "/")
            out[doc_id] = {"title": pdf.stem, "source_path": source_path, "group": group}
    return out


def backfill(cfg: Config, roots: list[Path] | None = None) -> dict[str, int]:
    """Set title/source_path/group on every document that has a source PDF
    under ``roots``. Returns {scanned, matched}. Only touches existing items."""
    found = scan_sources(roots or _DEFAULT_ROOTS)
    if not found:
        return {"scanned": 0, "matched": 0}
    store = _store(cfg)
    matched = 0
    for doc_id, meta in found.items():
        if store.point(doc_id, doc_id) is None:
            continue
        ops = [
            {"op": "set", "path": "/title", "value": meta["title"]},
            {"op": "set", "path": "/source_path", "value": meta["source_path"]},
        ]
        # A null write must REMOVE the property rather than store a null.
        ops.append({"op": "set", "path": "/group", "value": meta["group"]}
                   if meta["group"] is not None
                   else {"op": "remove", "path": "/group"})
        store.patch(doc_id, doc_id, ops)
        if store.point(f"{doc_id}:agreement", doc_id) is not None:
            store.patch(f"{doc_id}:agreement", doc_id,
                        [{"op": "set", "path": "/title", "value": meta["title"]}])
        matched += 1
    return {"scanned": len(found), "matched": matched}


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Backfill Document title/source_path/group from source PDFs.")
    ap.add_argument("roots", nargs="*", help="folders to scan (default: data/)")
    args = ap.parse_args()
    cfg = Config.load()
    roots = [Path(r) for r in args.roots] if args.roots else _DEFAULT_ROOTS
    res = backfill(cfg, roots)
    print(f"[DOCMETA] scanned={res['scanned']} matched={res['matched']} roots={[str(r) for r in roots]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
