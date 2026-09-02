"""rebuild_kb.py — the universal KB rollback lever.

Wipes the Cosmos container and rebuilds the whole knowledge base from the
cached extraction artifacts in storage/ (canonical/, doc/, doc_geometry/).
No LLM calls; embeddings come from the disk cache in storage/emb_cache/
(first run populates it, later runs are free).

Usage::

    .venv/Scripts/python -m scripts.rebuild_kb            # nuke + rebuild all
    .venv/Scripts/python -m scripts.rebuild_kb --dry-run  # counts only, no writes

Prints a before/after item-count comparison so a rebuild that silently
loses data is visible immediately.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

from dotenv import load_dotenv

from pipeline.config import Config
from pipeline.extraction import embed as embed_mod
from pipeline.extraction.pack import load as load_pack
from pipeline.ontology import DEFAULT_DOCTYPE
from pipeline.kb import docmeta as docmeta_mod
from pipeline.kb import km as km_mod
from pipeline.kb import load as load_mod
from pipeline.store.client import get_store


def _node_counts(cfg: Config) -> dict[str, int]:
    """Item count per label (facts count once per label, mirroring the old
    per-label node counts so the before/after diff stays comparable)."""
    store = get_store(cfg)
    try:
        store.ensure()
        rows = store.query("SELECT c.kind, c.label FROM c WHERE c.kind != 'edge'")
    except Exception:  # noqa: BLE001 — empty/unreachable store = zero counts
        return {}
    kind_label = {
        "document": "Document", "agreement": "Agreement", "section": "Section",
        "block": "Block", "evidence": "EvidenceSpan", "mention": "FactMention",
        "proposal": "Proposal", "opsfield": "OpsField", "docasset": "DocAsset",
    }
    out: dict[str, int] = {}
    for r in rows:
        kind = r.get("kind")
        if kind == "fact":
            out["Fact"] = out.get("Fact", 0) + 1
            lab = r.get("label")
            if lab:
                out[lab] = out.get(lab, 0) + 1
        elif kind in kind_label:
            out[kind_label[kind]] = out.get(kind_label[kind], 0) + 1
        elif kind in ("hub", "ext"):
            pass  # counted below by their sub-label
    for r in store.query("SELECT c.hub FROM c WHERE c.kind = 'hub'"):
        out[r["hub"]] = out.get(r["hub"], 0) + 1
    for r in store.query("SELECT c.ext FROM c WHERE c.kind = 'ext'"):
        out[r["ext"]] = out.get(r["ext"], 0) + 1
    return out


def _edge_count(cfg: Config) -> int:
    store = get_store(cfg)
    try:
        return store.count("SELECT VALUE COUNT(1) FROM c WHERE c.kind = 'edge'")
    except Exception:  # noqa: BLE001
        return 0


def _doc_ids(cfg: Config) -> list[str]:
    """Real doc_ids = a canonical/<id>.json that has a matching doc/<id>.json.

    Variant canonicals (e.g. ``<id>.unit.json`` from the unit-extract
    experiment) share the base doc's doc.json and have none of their own —
    they are NOT independently loadable documents, so skip them loudly
    rather than crash load_doc on a missing doc/<id>.json."""
    canonical_dir = cfg.storage_root / "canonical"
    doc_dir = cfg.storage_root / "doc"
    if not canonical_dir.exists():
        return []
    ids: list[str] = []
    for p in sorted(canonical_dir.glob("*.json")):
        if (doc_dir / f"{p.stem}.json").exists():
            ids.append(p.stem)
        else:
            print(f"[skip]  {p.name} — no doc/{p.stem}.json (variant canonical, not a doc)")
    return ids


def _print_counts(title: str, counts: dict[str, int], edges: int) -> None:
    print(f"\n--- {title} ---")
    for label, n in sorted(counts.items()):
        print(f"  {label:20s} {n}")
    print(f"  {'(edges)':20s} {edges}")
    print(f"  {'(total nodes)':20s} {sum(counts.values())}")


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Nuke + rebuild Neo4j KB from storage/ cache.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print current counts and the docs that would be loaded; no writes.")
    args = ap.parse_args()

    cfg = Config.load()
    doc_ids = _doc_ids(cfg)
    if not doc_ids:
        print("No docs in storage/canonical/ — nothing to rebuild.")
        return 1

    before = _node_counts(cfg)
    before_edges = _edge_count(cfg)
    _print_counts("BEFORE", before, before_edges)
    print(f"\ndocs in cache: {doc_ids}")

    if args.dry_run:
        return 0

    t0 = time.monotonic()

    pack = load_pack(DEFAULT_DOCTYPE)

    n = load_mod.nuke(cfg)
    print(f"\n[NUKE]  dropped container ({n} items)")

    load_mod.apply_ddl(cfg, pack)
    print("[DDL]   database + container ensured (vector policy where supported)")

    # Per doc: the structure tier (Document / Section / Block). Knowledge comes
    # from the ops-view fields and the KM layer below, not from graph facts.
    for doc_id in doc_ids:
        counts = load_mod.load_doc(cfg, doc_id)
        print(f"[LOAD]  {doc_id}: {sum(counts.values())} nodes")

    # Document metadata: human title + source path + contract-family group,
    # rebuilt deterministically from the source PDF folders. Powers document
    # naming/links + entity->family scoping. Idempotent.
    print(f"[DOCMETA] {docmeta_mod.backfill(cfg)}")

    # KM layer: materialise the verified ops fields into aligned knowledge —
    # :OpsField nodes (trust tier + evidence), :CanonicalParty hubs (resolve-and-link),
    # the declared document DAG, and per-field supersedence. Reads storage/fields +
    # storage/review (no LLM). No-op for docs without a schema-first extraction yet.
    print(f"[KM] {km_mod.build_km(cfg)}")

    # One event loop for all docs — per-doc asyncio.run leaves httpx
    # connections closing against a dead loop (noisy, harmless, but noisy).
    async def _embed_all() -> None:
        for doc_id in doc_ids:
            counts = await embed_mod.embed_doc(cfg, doc_id)
            print(f"[EMBED] {doc_id}: {counts}")

    asyncio.run(_embed_all())

    # Bump the build stamp — the API's in-RAM vector index reloads on this.
    get_store(cfg).set_build_stamp("rebuild_kb")

    after = _node_counts(cfg)
    after_edges = _edge_count(cfg)
    _print_counts("AFTER", after, after_edges)

    # Diff — labels that shrank are the thing to stare at.
    shrunk = {l: (before.get(l, 0), after.get(l, 0))
              for l in set(before) | set(after)
              if after.get(l, 0) < before.get(l, 0)}
    print(f"\nrebuild took {time.monotonic()-t0:.1f}s")
    # ASCII only — Windows consoles default to cp1252.
    if shrunk:
        print("\n[WARN] labels with FEWER nodes than before:")
        for l, (b, a) in sorted(shrunk.items()):
            print(f"  {l}: {b} -> {a}")
    else:
        print("no label lost nodes vs. before [OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
