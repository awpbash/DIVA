"""audit_fragility.py — read-only fragility dashboard over the canonical freeze.

Turns "extraction feels fragile" into numbers, deterministically and for free
(reads storage/canonical/*.json — no Docker, no Neo4j, no OpenAI). It separates
the two distinct failure modes we keep meeting one chat at a time:

  A. CAPTURED-BUT-UNLABELLED (mislabel -> strict-filter miss): the value+bbox
     extracted fine, but the free-text CLASS label landed in a lossy catch-all
     ('other'/null), so strict-filtered retrieval can't find it by concept.
     Measured for date concept (role), measure.normalized_parameter, event_type.

  B. WEAK / LOST PROVENANCE (drop risk): snippet_ok=false, low raw_confidence,
     or a source with no rects/bbox (the highlight would vanish). Plus the
     quarantine bucket (unknown-category facts — never loaded as nodes).

Run:  PYTHONUTF8=1 python -m scripts.audit_fragility
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from pipeline.config import Config

_GENERIC = {"other", "unknown", "misc", "miscellaneous", "general", "none", "", "n/a", "na"}
_DATE_CATS = {"date"}
_MEASURE_CATS = {"measurement", "money", "charge", "rate", "quantity", "measure"}
_EVENT_CATS = {"event"}
_WEAK_CONF = 0.6


def _is_generic(v) -> bool:
    return (v is None) or (str(v).strip().lower() in _GENERIC)


def _date_concept(f: dict) -> str | None:
    n = f.get("normalised") or {}
    # the loader types a date from its role; normalised.type is only absolute/
    # relative (temporal kind), never the concept. So concept == specific role.
    for cand in (f.get("role"), n.get("date_type")):
        if not _is_generic(cand):
            return str(cand)
    return None


def _bar(n: int, total: int, width: int = 28) -> str:
    filled = 0 if not total else round(width * n / total)
    return "[" + "#" * filled + "." * (width - filled) + "]"


def main() -> None:
    cfg = Config.load()
    cdir = cfg.storage_root / "canonical"
    files = sorted(cdir.glob("*.json"))
    if not files:
        print(f"no canonical files under {cdir}")
        return

    cat_total: Counter = Counter()
    # mode A — captured but unlabelled, per measurable family
    a_unlabelled = defaultdict(lambda: [0, 0])  # family -> [generic, total]
    a_doc_dates = defaultdict(int)              # doc -> generic date count
    # mode B — provenance weakness
    b_snippet_fail = b_low_conf = b_no_rect = b_facts = 0
    b_doc = defaultdict(int)

    for fp in files:
        doc = json.loads(fp.read_text(encoding="utf-8"))
        did = doc.get("doc_id", fp.stem)
        for f in doc.get("facts") or []:
            cat = f.get("category")
            cat_total[cat] += 1
            b_facts += 1

            # --- mode A ---
            if cat in _DATE_CATS:
                g = _date_concept(f) is None
                a_unlabelled["date.concept"][0] += int(g)
                a_unlabelled["date.concept"][1] += 1
                if g:
                    a_doc_dates[did] += 1
            elif cat in _MEASURE_CATS:
                p = (f.get("normalised") or {}).get("normalized_parameter")
                g = _is_generic(p)
                a_unlabelled["measure.normalized_parameter"][0] += int(g)
                a_unlabelled["measure.normalized_parameter"][1] += 1
            elif cat in _EVENT_CATS:
                et = (f.get("normalised") or {}).get("event_type")
                a_unlabelled["event.event_type"][0] += int(_is_generic(et))
                a_unlabelled["event.event_type"][1] += 1

            # --- mode B ---
            weak = False
            srcs = f.get("sources") or []
            if any(s.get("snippet_ok") is False for s in srcs):
                b_snippet_fail += 1; weak = True
            try:
                rc = float(f.get("raw_confidence") if f.get("raw_confidence") is not None
                           else f.get("confidence") or 0.0)
            except (TypeError, ValueError):
                rc = 0.0
            if rc < _WEAK_CONF:
                b_low_conf += 1; weak = True
            if srcs and all(not (s.get("rects") or s.get("bbox") or s.get("bboxes")) for s in srcs):
                b_no_rect += 1; weak = True
            if weak:
                b_doc[did] += 1

    # quarantine
    qdir = cfg.storage_root / "quarantine"
    q_files = list(qdir.glob("**/*")) if qdir.exists() else []
    q_facts = 0
    q_reasons: Counter = Counter()
    for qf in q_files:
        if qf.is_file():
            try:
                data = json.loads(qf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, list):
                q_facts += len(data)
            elif isinstance(data, dict):
                q_facts += int(data.get("n_drops") or len(data.get("drops") or data.get("quarantined") or []))
                for r, n in (data.get("by_reason") or {}).items():
                    q_reasons[r] += n

    total = sum(cat_total.values())
    print(f"\n=== EXTRACTION FRAGILITY AUDIT ===  docs={len(files)}  facts={total}\n")

    print("category mix:")
    for cat, n in cat_total.most_common():
        print(f"   {cat:<14} {n}")

    print("\n-- MODE A: captured but UNLABELLED (strict-filter blind) --")
    for fam, (g, t) in sorted(a_unlabelled.items()):
        pct = 0 if not t else round(100 * g / t)
        print(f"   {fam:<28} {_bar(g,t)} {g}/{t} generic ({pct}%)")
    if a_doc_dates:
        print("   worst docs for generic dates:")
        for did, n in sorted(a_doc_dates.items(), key=lambda x: -x[1])[:6]:
            print(f"      {did}: {n}")

    print("\n-- MODE B: weak / lost provenance (drop risk) --")
    print(f"   snippet_ok=false      {_bar(b_snippet_fail,b_facts)} {b_snippet_fail}/{b_facts}")
    print(f"   raw_confidence<{_WEAK_CONF}    {_bar(b_low_conf,b_facts)} {b_low_conf}/{b_facts}")
    print(f"   no rects/bbox at all  {_bar(b_no_rect,b_facts)} {b_no_rect}/{b_facts}")
    if b_doc:
        print("   worst docs for weak facts:")
        for did, n in sorted(b_doc.items(), key=lambda x: -x[1])[:6]:
            print(f"      {did}: {n}")

    print(f"\n-- quarantine (dropped, not loaded as nodes) --\n   {q_facts} drops across {len([f for f in q_files if f.is_file()])} files")
    for r, n in q_reasons.most_common():
        print(f"      {r}: {n}")
    print()


if __name__ == "__main__":
    main()
