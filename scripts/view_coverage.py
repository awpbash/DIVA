"""view_coverage.py — read-only: how much of the field schema the store can
actually fill, broken down by the mechanism each field is captured with.

Run:  PYTHONUTF8=1 python -m scripts.view_coverage

Free. Every statement is a plain Cosmos SQL SELECT and no model is called.

Reads the ACTIVE domain's schema and pack, so it reports on whatever this
instance is configured for. It used to walk one domain's fact labels and
categories by name, which meant a different domain got a page of zeroes and a
section header about a mechanism the framework no longer has.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from pipeline.config import Config
from pipeline.extraction.pack import load as load_pack
from pipeline.kb.opsview_spec import load as load_view
from pipeline.store.client import get_store


def main() -> None:
    cfg = Config.load()
    store = get_store(cfg)
    view = load_view()
    pack = load_pack()

    docs = store.query("SELECT c.doctype FROM c WHERE c.kind = 'document'")
    doctypes = Counter(r.get("doctype") for r in docs)
    print(f"\n=== DOCUMENTS ({len(docs)}) ===")
    for dt, n in sorted(doctypes.items(), key=lambda kv: -kv[1]):
        print(f"   {dt or 'untyped'}: {n}")

    # --- the schema, by capture mechanism ---------------------------------- #
    by_mech: dict[str, list] = defaultdict(list)
    for f in view.fields:
        by_mech[f.mechanism or "unset"].append(f)

    filled = Counter()
    total = Counter()
    for row in store.query(
            "SELECT c.full_key, c['value'] AS field_value FROM c "
            "WHERE c.kind = 'opsfield'"):
        key = row.get("full_key")
        if key:
            total[key] += 1
            if str(row.get("field_value") or "").strip():
                filled[key] += 1

    print(f"\n=== FIELD SCHEMA ({len(view.fields)} fields, "
          f"{len(by_mech)} mechanisms) ===")
    for mech in sorted(by_mech):
        fields = by_mech[mech]
        have = sum(1 for f in fields if filled.get(f.full_key))
        print(f"\n   {mech}  —  {have}/{len(fields)} filled somewhere")
        for f in sorted(fields, key=lambda x: x.full_key):
            n = filled.get(f.full_key, 0)
            mark = " " if n else "·"
            print(f"     {mark} {f.full_key:<52} {n} document(s)")

    # --- the raw material underneath --------------------------------------- #
    print("\n=== FACT LABELS IN THE STORE ===")
    counts = {}
    for label in sorted(pack.fact_labels):
        counts[label] = store.count(
            "SELECT VALUE COUNT(1) FROM c WHERE c.kind = 'fact' "
            "AND ARRAY_CONTAINS(c.labels, @l)", [{"name": "@l", "value": label}])
    width = max((len(x) for x in counts), default=10)
    for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"   {label:<{width}} {n}")
    if not any(counts.values()):
        print("   (nothing loaded yet — ingest a document first)")

    print("\n=== IDENTITY HUBS ===")
    for hub in sorted(pack.hubs):
        n = store.count(
            "SELECT VALUE COUNT(1) FROM c WHERE c.kind = 'hub' AND c.hub = @h",
            [{"name": "@h", "value": hub}])
        print(f"   {hub}: {n}")

    print("\n=== ORPHAN SPANS (the recall net under the typed facts) ===")
    orphans = store.count(
        "SELECT VALUE COUNT(1) FROM c WHERE c.kind = 'mention' "
        "AND c.surfaced = false")
    print(f"   raw spans that back no typed fact: {orphans}")
    print()


if __name__ == "__main__":
    main()
