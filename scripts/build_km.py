"""build_km.py — build the aligned KM layer on top of the loaded graph.

Materialises the verified ops fields into cross-document knowledge: :OpsField nodes
(with trust tier + evidence), :CanonicalParty hubs (resolve-and-link), the AMENDS/
SUPERSEDES/NOVATES document DAG, and per-field supersedence currency. Deterministic +
free (reads storage/fields + storage/review, no LLM). Idempotent — safe to re-run.

Runs AFTER the base graph exists (scripts.rebuild_kb loads Documents + docmeta + timeline);
this pass is also folded into rebuild_kb so a full rebuild produces the KM layer too.

    .venv/Scripts/python -m scripts.build_km            # build (needs Neo4j up)
    .venv/Scripts/python -m scripts.build_km --dry-run  # parse + report, no writes
"""
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from pipeline.config import Config
from pipeline.kb import km as km_mod


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Build the aligned KM layer from cache.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse the extraction/verification cache and report counts; no graph writes.")
    args = ap.parse_args()

    counts = km_mod.build_km(Config.load(), dry_run=args.dry_run)
    tag = "DRY-RUN" if args.dry_run else "BUILT"
    print(f"\n[KM {tag}]")
    for k, v in counts.items():
        print(f"  {k:18s} {v}")
    if not counts.get("ops_fields"):
        print("\nNo :OpsField written — run `python -m pipeline.kb.field_llm --all` first "
              "to produce schema-first extractions in storage/fields/.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
