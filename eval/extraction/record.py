"""eval/extraction/record.py — build a per-document REVIEW RECORD.

Every ops field for a document as the human-verification workflow needs it:
  * distinct VALUES, each clustering its own evidence — so multiple AGREEING
    mentions ("Deposit of $1,000" + "Deposit is defined as SGD 1,000…") sit under
    ONE value, while DISAGREEING mentions surface as separate values → `conflict`;
  * every evidence span tagged by KIND (definition / schedule / clause) + page +
    bbox rects, so a reviewer sees "the definition and the clause both say 1,000"
    and can click to the highlight;
  * a green/yellow/red/grey status for fast verify-all.

Deterministic + free (reads the live Cosmos store, no LLM). Fields are DERIVED from the
existing extraction (reuses the scorer's mapping); a schema-first LLM extraction
can later replace the fill step without changing this record shape or the UI.

  PYTHONUTF8=1 python -m eval.extraction.record --doc <id>    # one document
  PYTHONUTF8=1 python -m eval.extraction.record --all         # every document
Writes storage/review/<doc_id>.json.

The review API already shares this builder, so the app and the scorer read one
extraction path.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from pipeline.config import Config
from pipeline.kb.opsview_spec import OpsField, OpsView, load as load_view
from pipeline.store.client import get_store
from eval.extraction.score import _norm, _present, make_extractor

_ROOT = Path(__file__).resolve().parents[2]
_OUT = _ROOT / "storage" / "review"

# Fields that need human judgement even when confidently extracted.
_JUDGEMENT = {"enum", "presence_enum", "free_text"}
_DEFINITION_CUES = ("means", "defined as", "shall mean", "refers to", "definition of")


def _evidence_kind(e: dict) -> str:
    """Label the evidence so the reviewer sees WHERE the value is attested:
    a DEFINITION (establishes meaning + value), a SCHEDULE/table row, or a CLAUSE."""
    snip = (e.get("snippet") or "").lower()
    if any(k in snip for k in _DEFINITION_CUES):
        return "definition"
    if "schedule" in snip or any(":r" in str(b) for b in (e.get("blocks") or [])):
        return "schedule"
    return "clause"


def _tag(e: dict) -> dict:
    return {"snippet": (e.get("snippet") or "")[:240], "page": e.get("page"),
            "rects": e.get("rects"), "kind": _evidence_kind(e)}


def _values(ext: dict) -> list[dict]:
    """Distinct values each with its (kind-tagged) evidence. Agreeing mentions of
    the SAME value cluster together (n_mentions > 1); different values stay apart."""
    out: list[dict] = []
    for r in ext.get("raw") or []:
        if not isinstance(r, dict):
            continue
        if "value" in r:                                   # value / enum / equipment
            evs = [_tag(e) for e in (r.get("evidence") or []) if e.get("snippet")]
            out.append({"value": r["value"], "n_mentions": len(evs), "evidence": evs})
        elif r.get("span"):                                # free_text / presence clause
            out.append({"value": r["span"][:240], "n_mentions": 1,
                        "evidence": [_tag({"snippet": r["span"], "page": r.get("page")})]})
    if not out:                                            # presence fallback
        out = [{"value": v, "n_mentions": 0, "evidence": []} for v in (ext.get("values") or [])]
    return out


def _status(field: OpsField, ext: dict, conflict: bool) -> str:
    """grey = blank (Not Stated or missed?) · red = conflict OR value with no
    evidence (unverifiable) · yellow = needs judgement · green = confident + backed."""
    if conflict:
        return "red"
    if not ext["values"]:
        return "grey"
    if not ext["evidence_ok"] and field.mechanism != "external":
        return "red"
    return "yellow" if field.type in _JUDGEMENT else "green"


def build_record(store, doc_id: str, title: str, view: OpsView) -> dict:
    fields: dict[str, dict] = {}
    counts = {"green": 0, "yellow": 0, "red": 0, "grey": 0}
    get = make_extractor(store, doc_id)
    for f in view.fields:
        ext = get(f)
        values = _values(ext)
        distinct = {_norm(v["value"]) for v in values if _present(v.get("value"))}
        # A single-cardinality field with two DIFFERENT values = a conflict to resolve;
        # a multi-cardinality field legitimately holds many (each is a distinct item).
        conflict = f.multiplicity == 1 and len(distinct) > 1
        st = _status(f, ext, conflict)
        counts[st] += 1
        fields[f.full_key] = {
            "title": f.title, "category": f.category, "type": f.type,
            "method": f.mechanism, "multiplicity": f.multiplicity,
            "sensitivity": f.sensitivity,
            "status": st, "conflict": conflict, "verified": False,
            # Cap only as a runaway guard: real multi-value fields (equipment
            # lists run past 40 items) must reach the review screen whole.
            "values": values[:64],
        }
    return {"doc_id": doc_id, "title": title, "status_counts": counts, "fields": fields}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default=None, help="one doc_id")
    ap.add_argument("--all", action="store_true", help="every document")
    args = ap.parse_args()

    view = load_view()
    cfg = Config.load()
    store = get_store(cfg)
    _OUT.mkdir(parents=True, exist_ok=True)
    rows = store.query("SELECT c.doc_id, c.title FROM c WHERE c.kind = 'document'")
    docs = [{"id": r["doc_id"], "t": r.get("title") or r["doc_id"]} for r in rows]
    docs.sort(key=lambda d: d["t"])
    n = 0
    for d in docs:
        if not args.all and d["id"] != args.doc:
            continue
        rec = build_record(store, d["id"], d["t"], view)
        # Atomic replace: the review API reads these live, a torn record
        # breaks the whole review screen for the document.
        out = _OUT / f"{d['id']}.json"
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, out)
        c = rec["status_counts"]
        print(f"  {d['id']}  {d['t'][:44]:<44}  "
              f"green {c['green']:>2}  yellow {c['yellow']:>2}  red {c['red']:>2}  blank {c['grey']:>2}")
        n += 1
    if not n:
        print("no matching document (use --doc <id> or --all)")
    print(f"\nreview records → {_OUT}")


if __name__ == "__main__":
    main()
