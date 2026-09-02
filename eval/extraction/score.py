"""eval/extraction/score.py — measure how ACCURATELY the pipeline fills the
ops-view fields, per (document x field), against a hand-curated gold set.

This is the measure-first backbone. It is deterministic and free — it reads the
live Cosmos store (no LLM), projects each field via the mapping rules in the
ACTIVE domain's ops-view (``configs/views/<domain>_ops.yaml``), and grades the
result:

    correct             value matches gold
    correct_abstention  gold says "Not Stated" and extraction found nothing
    missed              gold has a value, extraction found nothing   (recall leak)
    wrong               extraction found a different value           (precision/tagging leak)
    hallucinated        gold says "Not Stated", extraction invented  (worst — trend to 0)

Plus an evidence-integrity flag: a "correct" field is only FULLY correct if its
value also carries a clause EvidenceSpan (the Prime Directive — every fact must
be verifiable). Only (doc, field) pairs present in gold are scored, so the
provisional gold can grow field-by-field without biasing the number.

Usage:
  PYTHONUTF8=1 python -m eval.extraction.score --run baseline        # score vs gold
  PYTHONUTF8=1 python -m eval.extraction.score --dump                # dump extractions (seed gold)
  PYTHONUTF8=1 python -m eval.extraction.score --dump --doc <doc_id>
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from pipeline.config import Config
from pipeline.extraction.textnorm import neutralize_markdown
from pipeline.kb import field_llm
from pipeline.ontology import resolve_active_doctype
from pipeline.store.client import get_store
from pipeline.kb.opsview_spec import OpsField, OpsView, load as load_view

_ROOT = Path(__file__).resolve().parents[2]
_DUMP_DIR = _ROOT / "eval" / "extraction" / "dumps"
_RESULTS = _ROOT / "eval" / "results"


def gold_dir(doctype: str | None = None) -> Path:
    """Where this domain's hand-curated gold lives.

    Per domain, because gold is bound to a specific corpus by document id and
    by field key. This used to be a hardcoded path naming the domain the
    framework was extracted from, which the export deliberately leaves behind,
    so a published copy pointed its own quality gate at a directory that does
    not exist and printed a private domain name while doing it.
    """
    return _ROOT / "eval" / "gold" / (doctype or resolve_active_doctype())


# A `_PARTY_PLACEHOLDERS` set used to sit here, listing one domain's role words
# so an unresolved mention would not be graded as a real answer. It had no
# callers, so it never graded anything. Removed rather than wired in: making it
# live now would change the accuracy number, and that change belongs in a
# deliberate scoring decision with a before and after, not in a cleanup.

_NOT_STATED = "Not Stated"
OUTCOMES = ["correct", "correct_abstention", "missed", "wrong", "hallucinated"]


# --------------------------------------------------------------------------- #
# Value helpers
# --------------------------------------------------------------------------- #
def _norm(s) -> str:
    """Casefold on top of the shared reader-artifact normalizer, so BOTH
    sides of every gold-vs-extracted comparison are markdown-neutral (CU
    table pipes, separator rows, HTML scaffolding, escapes, unicode
    variants). Whitespace collapse comes from the normalizer."""
    return neutralize_markdown(str(s or "")).lower()


_NUM = re.compile(r"-?\d[\d.]*")


def _nums(s) -> list[float]:
    """ALL numbers in a string. Strip thousands separators (comma before a 3-digit
    group), then treat any remaining comma as a decimal point — OCR renders "4.5"
    as "4,5" (European). Scanning all numbers lets a combined value string
    ("Cooling 0.0241/RTh; Chilled 0.0876/RTh") match a multi-value gold."""
    # Markdown-neutral first, so a value quoted out of a CU table row
    # ("| 580,000 RTh |") yields the same numbers as its gold twin.
    t = re.sub(r"(?<=\d),(?=\d{3}(\D|$))", "", neutralize_markdown(str(s or "")))
    t = t.replace(",", ".")
    out: list[float] = []
    for m in _NUM.finditer(t):
        try:
            out.append(float(m.group().rstrip(".")))
        except ValueError:
            pass
    return out


def _num(s):
    ns = _nums(s)
    return ns[0] if ns else None


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1e-6, 0.01 * max(abs(a), abs(b)))


def _present(v) -> bool:
    if v is None:
        return False
    if isinstance(v, (list, tuple)):
        return any(_present(x) for x in v)
    return _norm(v) not in ("", _norm(_NOT_STATED))


def _contains(g: str, e: str) -> bool:
    ng, ne = _norm(g), _norm(e)
    if not ng or not ne:
        return False
    if ng in ne or ne in ng:
        return True
    gt, et = set(ng.split()), set(ne.split())
    return len(gt & et) >= max(2, len(gt) // 2)   # >=half of gold tokens present


def _match(field: OpsField, gold_val, ext_values: list[str]) -> bool:
    golds = gold_val if isinstance(gold_val, list) else [gold_val]
    golds = [g for g in golds if _present(g)]
    exts = [str(e) for e in ext_values if _present(e)]
    if not golds:
        return not exts
    if field.type in ("enum", "presence_enum"):
        E = {_norm(e) for e in exts}
        return all(_norm(g) in E for g in golds)
    if field.type in ("value", "number"):
        ext_nums = [n for e in exts for n in _nums(e)]   # every number across all extractions
        for g in golds:
            gn = _num(g)
            if gn is None:
                if not any(_contains(g, e) for e in exts):
                    return False
            elif not any(_close(gn, x) for x in ext_nums):
                return False
        return True
    # text / free_text / reference
    return all(any(_contains(g, e) for e in exts) for g in golds)


# --------------------------------------------------------------------------- #
# Filling the schema for one document
# --------------------------------------------------------------------------- #
# There used to be a second arm here: one branch per source mechanism, each
# projecting a field out of the graph, over a per-document fetch cache. It read
# the open-vocabulary fact tier, which was retired, so it had been unreachable
# for a while. Schema-first extraction is the one path now.




def make_extractor(store, doc_id: str):
    """Return a `field -> {values, evidence_ok, raw}` filler.

    Reads the schema-first extraction (pipeline.kb.field_llm). ``store`` is
    accepted for signature stability but no longer queried: the open-vocab fact
    tier the old `derive` arm projected from was retired (OPEN_SOURCE_PLAN.md
    decision 5), so there is one extraction path to score.

    `external` fields score as empty on purpose. They live outside the document
    body by definition, so no extractor can fill them and abstention is the
    honest result.
    """
    cache = field_llm.load(doc_id)

    def get(field: OpsField) -> dict:
        if field.mechanism == "external":
            return {"values": [], "evidence_ok": False, "raw": []}
        return field_llm.field_extract(cache, field)

    return get


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #
def _docs(store, only: str | None) -> list[dict]:
    rows = store.query(
        "SELECT c.doc_id, c.title, c['group'] AS grp FROM c WHERE c.kind = 'document'")
    out = [{"id": r["doc_id"], "title": r.get("title") or r["doc_id"],
            "grp": r.get("grp")} for r in rows]
    # Match the old ORDER BY grp, title with nulls last.
    out.sort(key=lambda r: (r["grp"] is None, r["grp"] or "", r["title"]))
    return [r for r in out if (only is None or r["id"] == only)]


def _load_gold(gold_root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not gold_root.exists():
        return out
    for p in sorted(gold_root.glob("*.json")):
        if p.name.startswith("_"):
            continue
        g = json.loads(p.read_text(encoding="utf-8"))
        if g.get("doc_id"):
            out[g["doc_id"]] = g
    return out


def run_dump(view: OpsView, store, only: str | None) -> None:
    _DUMP_DIR.mkdir(parents=True, exist_ok=True)
    for d in _docs(store, only):
        doc = d["id"]
        get = make_extractor(store, doc)
        fields_out: dict[str, dict] = {}
        for f in view.fields:
            ext = get(f)
            if ext["values"]:
                fields_out[f.full_key] = {
                    "extracted": ext["values"][:8],
                    "evidence_ok": ext["evidence_ok"],
                }
        out = {"doc_id": doc, "title": d["title"], "group": d["grp"],
               "n_fields_with_extraction": len(fields_out), "fields": fields_out}
        (_DUMP_DIR / f"{doc}.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  {doc}  {d['title'][:55]:<55}  {len(fields_out):>2} fields extracted")
    print(f"\nDumps written to {_DUMP_DIR}")


def run_score(view: OpsView, store, run_name: str, only: str | None,
              gold_root: Path | None = None) -> None:
    gold_root = gold_root or gold_dir()
    gold = _load_gold(gold_root)
    if not gold:
        print(
            f"No gold files in {gold_root}.\n"
            "\n"
            "Nothing to score yet, which is expected on a fresh install: a gold\n"
            "set is hand-curated against one specific corpus, so it cannot ship\n"
            "with the framework. To create one:\n"
            "\n"
            "  1. python -m eval.extraction.score --dump\n"
            f"  2. correct the dumped values by hand into {gold_root}\n"
            "  3. re-run this command\n"
        )
        return
    by_field = {f.full_key: f for f in view.fields}
    rows: list[dict] = []
    for doc_id, g in gold.items():
        if only and doc_id != only:
            continue
        get = make_extractor(store, doc_id)
        for fkey, gentry in (g.get("fields") or {}).items():
            field = by_field.get(fkey) or view.field(fkey)
            if field is None:
                continue
            gold_val = gentry.get("value") if isinstance(gentry, dict) else gentry
            ext = get(field)
            outcome = _classify(field, ext["values"], gold_val)
            rows.append({
                "doc_id": doc_id, "field": field.full_key, "category": field.category,
                "type": field.type, "mechanism": field.mechanism, "outcome": outcome,
                "evidence_ok": ext["evidence_ok"],
                "extracted": ext["values"][:6], "gold": gold_val,
            })
    _report(rows, run_name, view)


def _classify(field: OpsField, ext_values: list[str], gold_val) -> str:
    gp, ep = _present(gold_val), bool([e for e in ext_values if _present(e)])
    if not gp and not ep:
        return "correct_abstention"
    if not gp and ep:
        return "hallucinated"
    if gp and not ep:
        return "missed"
    return "correct" if _match(field, gold_val, ext_values) else "wrong"


def _report(rows: list[dict], run_name: str, view: OpsView) -> None:
    run_dir = _RESULTS / f"extraction_{run_name}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

    n = len(rows)
    by_outcome = Counter(r["outcome"] for r in rows)
    correct = by_outcome["correct"] + by_outcome["correct_abstention"]
    ev_ok = sum(1 for r in rows if r["outcome"] == "correct" and r["evidence_ok"])
    n_value_correct = by_outcome["correct"]
    acc = 100 * correct / n if n else 0

    # per-category
    cats: dict[str, Counter] = {}
    for r in rows:
        cats.setdefault(r["category"], Counter())[r["outcome"]] += 1

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# extraction scorecard — {run_name}", "",
             f"_generated {ts} · {n} (doc x field) scored · gold is PROVISIONAL_", "",
             "## Headline", "",
             f"- **Accuracy (correct + correct-abstention): {acc:.0f}%**  ({correct}/{n})",
             f"- Missed (recall leak): **{by_outcome['missed']}**",
             f"- Wrong (precision/tagging leak): **{by_outcome['wrong']}**",
             f"- Hallucinated (must be ~0): **{by_outcome['hallucinated']}**",
             f"- Evidence-backed among value-correct: {ev_ok}/{n_value_correct}"
             + (f" ({100*ev_ok/n_value_correct:.0f}%)" if n_value_correct else ""),
             "", "## By category", "",
             "| Category | scored | correct | missed | wrong | halluc |",
             "|---|--:|--:|--:|--:|--:|"]
    for cat, c in sorted(cats.items()):
        tot = sum(c.values())
        cok = c["correct"] + c["correct_abstention"]
        lines.append(f"| {view.category_titles.get(cat, cat)} | {tot} | {cok} | "
                     f"{c['missed']} | {c['wrong']} | {c['hallucinated']} |")

    halluc = [r for r in rows if r["outcome"] == "hallucinated"]
    if halluc:
        lines += ["", "## Hallucinations (gold = Not Stated, extraction invented a value)", ""]
        for r in halluc[:25]:
            lines.append(f"- `{r['field']}` [{r['doc_id']}] → {r['extracted']}")

    wrong = [r for r in rows if r["outcome"] == "wrong"]
    if wrong:
        lines += ["", "## Wrong (extraction ≠ gold)", ""]
        for r in wrong[:25]:
            lines.append(f"- `{r['field']}` [{r['doc_id']}] gold={r['gold']!r} got={r['extracted']}")

    (run_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"\n=== extraction scorecard: {run_name} ===")
    print(f"  scored {n} (doc x field)")
    for o in OUTCOMES:
        print(f"    {o:<20} {by_outcome[o]:>4}")
    print(f"  accuracy {acc:.0f}%  |  hallucinations {by_outcome['hallucinated']}  |  "
          f"written to {run_dir}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="baseline", help="run name (output dir suffix)")
    ap.add_argument("--doc", default=None, help="limit to one doc_id")
    ap.add_argument("--dump", action="store_true", help="dump extractions (seed gold) instead of scoring")
    ap.add_argument("--gold", default=None, type=Path,
                    help="gold directory (default: eval/gold/<active domain>)")
    args = ap.parse_args()

    view = load_view()
    cfg = Config.load()
    store = get_store(cfg)
    if args.dump:
        run_dump(view, store, args.doc)
    else:
        run_score(view, store, args.run, args.doc, gold_root=args.gold)


if __name__ == "__main__":
    main()
