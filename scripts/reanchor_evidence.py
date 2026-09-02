"""Re-anchor stored field_llm evidence rects from their cited blocks — free.

The extractor stores the block ids the model cited per evidence entry, and the
rects are a pure projection of those ids. When the citation filter in
``field_llm._resolve_evidence`` changes (e.g. the junk-sibling drop rule), this
script re-applies it to every cached extraction WITHOUT any LLM call, then the
usual ``rebuild_kb`` propagates the corrected rects to the graph and the
review records.

Run:  python -m scripts.reanchor_evidence [--doc DOC_ID] [--dry-run]
"""
from __future__ import annotations

import argparse
import json

from pipeline.config import Config
from pipeline.kb.field_llm import _cache_dir, _load_grids, _load_pages, _render, _resolve_evidence


def reanchor_doc(cfg: Config, doc_id: str, dry: bool = False) -> tuple[int, int]:
    """Returns (entries_changed, rects_dropped) for one document."""
    cache = _cache_dir(cfg) / f"{doc_id}.json"
    pages = _load_pages(cfg, doc_id)
    if not cache.exists() or not pages:
        return (0, 0)
    _text, block_map, pages_by_no = _render(pages, _load_grids(cfg, doc_id))
    rec = json.loads(cache.read_text(encoding="utf-8"))
    changed = dropped = 0
    for entry in (rec.get("fields") or {}).values():
        for v in entry.get("values") or []:
            for e in v.get("evidence") or []:
                if not e.get("blocks"):
                    continue
                probe = {"value": v.get("value"), "snippet": e.get("snippet"),
                         "page": e.get("page"), "blocks": e.get("blocks")}
                new_ev, _ok = _resolve_evidence(probe, block_map, pages_by_no)
                if new_ev["rects"] != e.get("rects") or new_ev["page"] != e.get("page"):
                    n_old = len(json.loads(e["rects"])) if e.get("rects") else 0
                    n_new = len(json.loads(new_ev["rects"])) if new_ev["rects"] else 0
                    dropped += max(0, n_old - n_new)
                    changed += 1
                    if not dry:
                        e["rects"] = new_ev["rects"]
                        e["page"] = new_ev["page"]
    if changed and not dry:
        cache.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    return (changed, dropped)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--doc", help="single doc_id (default: every cached extraction)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = Config.load()
    docs = [args.doc] if args.doc else sorted(p.stem for p in _cache_dir(cfg).glob("*.json"))
    tot_c = tot_d = 0
    for doc_id in docs:
        c, d = reanchor_doc(cfg, doc_id, dry=args.dry_run)
        tot_c += c
        tot_d += d
        if c:
            print(f"  {doc_id}  entries changed {c:4d}  junk rects dropped {d:4d}")
    verb = "would change" if args.dry_run else "changed"
    print(f"\n{verb} {tot_c} evidence entries, dropped {tot_d} junk rects "
          f"across {len(docs)} docs")
    if tot_c and not args.dry_run:
        print("now run: python -m scripts.rebuild_kb")


if __name__ == "__main__":
    main()
