"""reconcile_kb.py — deterministic, READ-ONLY reconciliation audit of the KB layers.

Three layers describe the same corpus and this module cross-checks them, no LLM:

  1. ONTOLOGY  — :OpsField nodes (schema-first extraction, pipeline/kb/km.py)
  2. LEGACY    — :Fact type-labelled nodes -[:SUPPORTED_BY]-> :EvidenceSpan
  3. RAW TEXT  — :Block / :Section nodes

plus both document-family layers (legacy Agreement-level timeline edges vs the
KM Document-level DAG). Eight audit classes:

  a. coverage census            e. sensitivity net audit (leak guard)
  b. ontology-miss candidates   f. family/DAG agreement
  c. cross-layer conflicts      g. trust census
  d. reverse evidence integrity h. corroboration rate (the headline 2x2)

Everything keys off ``distinctive_value_tokens`` (pipeline.kb.km) — precise value
tokens (decimals, grouped amounts, 5+ digit runs, emails), never bare small ints,
compared after numeric normalisation ("41.8500" == "41.85", "550,000" == "550000")
so formatting differences between layers don't fake misses.

READ-ONLY: every store query is a plain Cosmos SQL SELECT, asserted free of
write verbs at startup, and the connection is refused unless the Cosmos URI is
local (a cloud account is a protected golden copy, never touched).

    PYTHONUTF8=1 .venv/Scripts/python -m scripts.reconcile_kb

Outputs: eval/reconcile/reconcile_report.md (human) + reconcile.json (machine),
and the headline numbers on the console.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parents[1]
load_dotenv(_REPO / ".env")

from pipeline.config import Config                                  # noqa: E402
from pipeline.kb.km import _policy_financials, distinctive_value_tokens  # noqa: E402
from pipeline.kb.opsview_spec import load as load_view              # noqa: E402
from pipeline.storage import fields_dir                             # noqa: E402
from pipeline.store.client import get_store                         # noqa: E402

OUT_DIR = _REPO / "eval" / "reconcile"

# An optional self-test: name a document and a field you KNOW extraction
# dropped, and the report says whether this audit still catches it. Corpus
# specific by nature, so it is set per instance and skipped when unset. It was
# hardcoded to one private document's hash, which meant every other corpus got
# a permanent "matcher broken" verdict on a case it had never contained.
#
#   RECONCILE_SEED_DOC=<doc_id prefix>  RECONCILE_SEED_FIELD=<field key>
_SEEDED_DOC_PREFIX = os.environ.get("RECONCILE_SEED_DOC", "").strip()
_SEEDED_FIELD = os.environ.get("RECONCILE_SEED_FIELD", "").strip()
_SEEDED_ON = bool(_SEEDED_DOC_PREFIX and _SEEDED_FIELD)

# --------------------------------------------------------------------------- #
# Store reads. Every statement is a plain Cosmos SQL SELECT, asserted below.
# The old multi-step Cypher joins (fact-to-evidence, label census, edge level
# split) are rebuilt in Python inside pull().
# --------------------------------------------------------------------------- #
_DAG_RELS = ["AMENDS", "SUPERSEDES", "NOVATES"]

Q = {
    "docs": ("SELECT c.doc_id, c.title, c['group'] AS grp, c.effective_date, "
             "c.extraction_mode FROM c WHERE c.kind = 'document'"),
    "ops": "SELECT * FROM c WHERE c.kind = 'opsfield'",
    "facts": ("SELECT c.id, c.doc_id, c.labels, c.normalized_parameter "
              "FROM c WHERE c.kind = 'fact'"),
    "evidence": ("SELECT c.id, c.fact_id, c.text_span, c.page_no "
                 "FROM c WHERE c.kind = 'evidence' AND IS_DEFINED(c.fact_id)"),
    "blocks": ("SELECT c.id, c.block_id, c.doc_id, c.page_no, c.text, c.sensitivity "
               "FROM c WHERE c.kind = 'block'"),
    "sections": "SELECT c.doc_id FROM c WHERE c.kind = 'section'",
    "dag_edges": ("SELECT c.src, c.rel, c.tgt, c.via FROM c "
                  "WHERE c.kind = 'edge' AND ARRAY_CONTAINS(@rels, c.rel)"),
    "proposals": ("SELECT c.doc_id, c.relation, c.status, c.reason FROM c "
                  "WHERE c.kind = 'proposal' AND c.proposal_kind = 'supersedence'"),
}

_WRITE_CLAUSE = re.compile(r"\b(INSERT|UPSERT|UPDATE|DELETE|REPLACE)\b",
                           re.IGNORECASE)


def _assert_read_only() -> None:
    for name, q in Q.items():
        if _WRITE_CLAUSE.search(q):
            raise RuntimeError(f"query {name!r} contains a write clause, audit aborted")


def _assert_local(cfg: Config, allow_live: bool = False) -> None:
    if "localhost" not in cfg.cosmos_uri and "127.0.0.1" not in cfg.cosmos_uri:
        if allow_live:
            print(f"[reconcile] auditing LIVE account {cfg.cosmos_uri} (read-only)")
            return
        raise RuntimeError(
            f"refusing non-local Cosmos URI {cfg.cosmos_uri!r} without --live: "
            "pass --live to audit a cloud account (the audit itself is read-only)")


# --------------------------------------------------------------------------- #
# Token machinery — distinctive value tokens, compared numerically normalised.
# --------------------------------------------------------------------------- #
def norm_token(tok: str) -> str:
    """Format-invariant comparison key for one distinctive token: numeric tokens
    lose grouping commas and trailing zeros ("41.8500"→"41.85", "550,000"→"550000");
    emails lowercase. Keeps cross-layer matching honest when the layers format the
    same value differently."""
    t = tok.strip()
    if "@" in t:
        return t.lower()
    bare = t.replace(",", "")
    try:
        return format(float(bare), ".10g")
    except ValueError:
        return bare


def toks(text: str | None) -> frozenset[str]:
    """Normalised distinctive tokens of a text ('' → empty set)."""
    return frozenset(norm_token(t) for t in distinctive_value_tokens(text or ""))


# Legal cross-reference cues: a decimal right after "Section"/"Clause"/"Ver." is a
# citation ("Section 10.3(c)"), not a value — generic contract-text hygiene, no
# per-document heuristics.
_REF_CUE = re.compile(r"(?:section|clause|article|paragraph|§|s\.|ver(?:sion)?\.?)[\s:]*$",
                      re.IGNORECASE)


def span_value_tokens(span: str | None) -> frozenset[str]:
    """Like ``toks`` but for legacy evidence spans: a token whose EVERY occurrence
    sits right after a cross-reference cue is dropped (it is a section number)."""
    s = span or ""
    out: set[str] = set()
    for raw in distinctive_value_tokens(s):
        keep = False
        for m in re.finditer(re.escape(raw), s):
            if not _REF_CUE.search(s[max(0, m.start() - 14): m.start()]):
                keep = True
                break
        if keep:
            out.add(norm_token(raw))
    return frozenset(out)


def trunc(s: str | None, n: int = 120) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


_WORD_SPLIT = re.compile(r"[^a-z0-9]+")
_STOP = {"of", "the", "a", "an", "to", "in", "if", "any", "no", "for", "e", "g", "per", ""}


def _words(*parts: str) -> set[str]:
    """Lowercase word set (plural-folded) for keyword matching."""
    out: set[str] = set()
    for p in parts:
        for w in _WORD_SPLIT.split(str(p or "").lower()):
            if w in _STOP:
                continue
            out.add(w[:-1] if w.endswith("s") and len(w) > 3 else w)
    return out


# --------------------------------------------------------------------------- #
# Field guessing — which ontology field a legacy fact's value belongs to.
# --------------------------------------------------------------------------- #
def build_field_matchers(view) -> tuple[list[dict], list[dict]]:
    """(exact param matchers from `value`-mechanism sources, keyword profiles)."""
    exact, keyword = [], []
    for f in view.fields:
        src = f.source or {}
        match = src.get("match") or {}
        if f.mechanism == "value":
            exact.append({
                "full_key": f.full_key, "label": src.get("label"),
                "any": {str(x).lower() for x in match.get("parameter_any") or []},
                "prefix": [str(x).lower() for x in match.get("parameter_prefix") or []],
            })
        kw = _words(f.key, f.title)
        for lst in (match.get("parameter_any"), match.get("parameter_prefix")):
            for x in lst or []:
                kw |= _words(str(x).replace("_", " "))
        keyword.append({"full_key": f.full_key, "words": kw})
    return exact, keyword


def guess_field(label: str, param: str, span: str,
                exact: list[dict], keyword: list[dict]) -> tuple[str | None, str]:
    """Best-effort ontology home for a legacy fact: (full_key, method).
    1) the view's own value-source mapping (label + parameter_any/prefix) — precise;
    2) keyword overlap between the fact's parameter/label and field key/title words."""
    p = (param or "").lower()
    for m in exact:
        if m["label"] == label and (p in m["any"] or any(p.startswith(x) for x in m["prefix"])):
            return m["full_key"], "view_mapping"
    fact_words = _words(param.replace("_", " "), label)
    best, best_score = None, 0
    for k in keyword:
        score = len(fact_words & k["words"])
        if score > best_score:
            best, best_score = k["full_key"], score
    if best and best_score >= 1:
        return best, f"keyword_overlap({best_score})"
    return None, "none"


# --------------------------------------------------------------------------- #
# Data pull
# --------------------------------------------------------------------------- #
def pull(cfg: Config) -> dict:
    """Fetch the raw item kinds once each, then rebuild in Python the exact row
    shapes the old Cypher statements returned, so the audit below is unchanged."""
    store = get_store(cfg)
    raw = {name: store.query(
               q, [{"name": "@rels", "value": _DAG_RELS}] if "@rels" in q else None)
           for name, q in Q.items()}

    docs = [{"doc_id": r["doc_id"], "title": r.get("title") or r["doc_id"],
             "grp": r.get("grp"), "effective_date": r.get("effective_date"),
             "extraction_mode": r.get("extraction_mode")} for r in raw["docs"]]
    docs.sort(key=lambda d: (d["grp"] or d["doc_id"], d["doc_id"]))

    ops = [{
        "ops_id": o.get("ops_id") or o["id"], "doc_id": o.get("doc_id"),
        "full_key": o.get("full_key"), "category": o.get("category"),
        "field_key": o.get("field_key"), "title": o.get("title"),
        "value": o.get("value") if o.get("value") is not None else "",
        "values": o.get("values"),
        "snippet": o.get("snippet") if o.get("snippet") is not None else "",
        "page": o.get("page"), "numbers": o.get("numbers"),
        "trust": o.get("trust") or "",
        "sensitivity": o.get("sensitivity") or "general",
        "is_current": o.get("is_current", True),
        "superseded_by": o.get("superseded_by"),
        "has_evidence": bool(o.get("has_evidence", False)),
    } for o in raw["ops"]]
    ops.sort(key=lambda o: o["ops_id"] or "")

    # Fact-with-evidence rows: the old SUPPORTED_BY join, now evidence.fact_id.
    facts_by_id = {f["id"]: f for f in raw["facts"]}
    fact_ev = []
    for e in sorted(raw["evidence"], key=lambda r: r["id"]):
        f = facts_by_id.get(e.get("fact_id"))
        if f is None:
            continue
        fact_ev.append({
            "doc_id": f.get("doc_id"),
            "labels": [l for l in (f.get("labels") or []) if l != "Fact"],
            "param": f.get("normalized_parameter") or "",
            "span": e.get("text_span") or "",
            "page": e.get("page_no"),
        })

    census: Counter = Counter()
    for f in raw["facts"]:
        for l in (f.get("labels") or []):
            if l != "Fact":
                census[(f.get("doc_id"), l)] += 1
    fact_label_census = [
        {"doc_id": d, "label": l, "c": c}
        for (d, l), c in sorted(census.items(), key=lambda kv: (kv[0][0] or "", kv[0][1]))]

    blocks = [{"block_id": b.get("block_id") or b["id"], "doc_id": b.get("doc_id"),
               "page": b.get("page_no"), "text": b.get("text") or "",
               "sensitivity": b.get("sensitivity")} for b in raw["blocks"]]
    blocks.sort(key=lambda b: b["block_id"] or "")

    sec_counts = Counter(r.get("doc_id") for r in raw["sections"])
    sections = [{"doc_id": d, "c": c}
                for d, c in sorted(sec_counts.items(), key=lambda kv: kv[0] or "")]

    # One edge fetch, split by level: Agreement-level ids carry ':agreement',
    # Document-level (KM DAG) ids are bare doc ids.
    legacy_edges, km_edges = [], []
    for e in raw["dag_edges"]:
        src, tgt = str(e.get("src") or ""), str(e.get("tgt") or "")
        row_via = e.get("via") or ""
        if src.endswith(":agreement"):
            legacy_edges.append({
                "src": src[: -len(":agreement")],
                "rel": e.get("rel"),
                "tgt": tgt[: -len(":agreement")] if tgt.endswith(":agreement") else tgt,
                "via": row_via})
        else:
            km_edges.append({"src": src, "rel": e.get("rel"), "tgt": tgt,
                             "via": row_via})
    legacy_edges.sort(key=lambda e: (e["src"], e["rel"], e["tgt"]))
    km_edges.sort(key=lambda e: (e["src"], e["rel"], e["tgt"]))

    proposals = [{"doc_id": p.get("doc_id"), "relation": p.get("relation"),
                  "status": p.get("status"), "reason": p.get("reason") or ""}
                 for p in raw["proposals"]]
    proposals.sort(key=lambda p: (p["doc_id"] or "", p["relation"] or ""))

    return {"docs": docs, "ops": ops, "facts": fact_ev,
            "fact_label_census": fact_label_census, "blocks": blocks,
            "sections": sections, "legacy_edges": legacy_edges,
            "km_edges": km_edges, "proposals": proposals}


def load_extraction_keys(cfg: Config, doc_id: str) -> tuple[set[str], set[str]]:
    """(field keys with >=1 surviving value, keys present but value-empty) in the
    schema-first extraction cache — distinguishes 'explicitly seen, Not Stated'
    from 'absent from the extraction output entirely'."""
    p = fields_dir(cfg) / f"{doc_id}.json"
    if not p.exists():
        return set(), set()
    try:
        fields = (json.loads(p.read_text(encoding="utf-8")).get("fields") or {})
    except json.JSONDecodeError:
        return set(), set()
    filled, empty = set(), set()
    for k, entry in fields.items():
        vals = [v for v in (entry or {}).get("values") or []
                if str(v.get("value") or "").strip()
                and str(v.get("value")).strip().lower() != "not stated"]
        (filled if vals else empty).add(k)
    return filled, empty


# --------------------------------------------------------------------------- #
# The audit
# --------------------------------------------------------------------------- #
# Fact labels considered VALUE-BEARING for miss detection: live labels whose name
# matches these stems (discovered at runtime, not hard-coded to today's pack).
_VALUE_LABEL_STEMS = ("charge", "rate", "cost", "fee", "amount", "quant", "percent",
                      "measure", "formula", "duration", "date", "deposit", "money",
                      "price", "tariff", "payment")


def value_bearing_labels(live_labels: set[str]) -> list[str]:
    return sorted(l for l in live_labels
                  if any(s in l.lower() for s in _VALUE_LABEL_STEMS))


def run_audit(cfg: Config) -> dict:
    view = load_view()
    view_keys = {f.full_key for f in view.fields}
    view_by_cat: dict[str, list] = view.by_category()
    conf_fields = {f.full_key for f in view.fields if f.sensitivity == "confidential"}
    exact_m, keyword_m = build_field_matchers(view)

    data = pull(cfg)
    docs = data["docs"]
    doc_ids = [d["doc_id"] for d in docs]
    doc_meta = {d["doc_id"]: d for d in docs}

    # ---- index the three layers per document -------------------------------
    ops_by_doc: dict[str, list[dict]] = defaultdict(list)
    for o in data["ops"]:
        o["tok_value"] = toks(o["value"]) | frozenset(
            t for v in (o["values"] or []) for t in toks(v))
        o["tok_snip"] = toks(o["snippet"])
        ops_by_doc[o["doc_id"]].append(o)

    doc_ops_value_toks = {d: frozenset(t for o in ops_by_doc[d] for t in o["tok_value"])
                          for d in doc_ids}
    doc_ops_all_toks = {d: doc_ops_value_toks[d]
                        | frozenset(t for o in ops_by_doc[d] for t in o["tok_snip"])
                        for d in doc_ids}

    facts_by_doc: dict[str, list[dict]] = defaultdict(list)
    live_labels: set[str] = set()
    for f in data["facts"]:
        f["tok_span"] = span_value_tokens(f["span"])   # cross-ref numbers filtered
        f["label"] = (f["labels"] or ["?"])[0]
        live_labels.update(f["labels"] or [])
        facts_by_doc[f["doc_id"]].append(f)
    doc_fact_toks = {d: frozenset(t for f in facts_by_doc.get(d, []) for t in f["tok_span"])
                     for d in doc_ids}

    blocks_by_doc: dict[str, list[dict]] = defaultdict(list)
    for b in data["blocks"]:
        b["tok"] = toks(b["text"])
        blocks_by_doc[b["doc_id"]].append(b)
    doc_block_toks = {d: frozenset(t for b in blocks_by_doc.get(d, []) for t in b["tok"])
                      for d in doc_ids}

    fact_census: dict[str, dict[str, int]] = defaultdict(dict)
    for r in data["fact_label_census"]:
        fact_census[r["doc_id"]][r["label"]] = r["c"]
    section_counts = {r["doc_id"]: r["c"] for r in data["sections"]}

    # ---- a. coverage census -------------------------------------------------
    coverage = []
    for d in doc_ids:
        filled_keys = {o["full_key"] for o in ops_by_doc[d]}
        json_filled, json_empty = load_extraction_keys(cfg, d)
        by_cat = {c: {"total": len(fs),
                      "filled": sum(1 for f in fs if f.full_key in filled_keys)}
                  for c, fs in view_by_cat.items()}
        coverage.append({
            "doc_id": d, "title": trunc(doc_meta[d]["title"], 60),
            "group": doc_meta[d]["grp"],
            "fields_total": len(view_keys),
            "fields_filled": len(filled_keys),
            "explicit_empty_in_cache": sorted(json_empty),
            "absent_from_extraction": sorted(view_keys - json_filled - json_empty),
            "n_absent": len(view_keys - json_filled - json_empty),
            "human_validated": sum(1 for o in ops_by_doc[d] if o["trust"] == "human_validated"),
            "machine_extracted": sum(1 for o in ops_by_doc[d] if o["trust"] == "machine_extracted"),
            "by_category": by_cat,
            "legacy_facts_by_label": fact_census.get(d, {}),
            "n_blocks": len(blocks_by_doc.get(d, [])),
            "n_sections": section_counts.get(d, 0),
        })

    # ---- b. ontology-miss candidates ----------------------------------------
    vlabels = value_bearing_labels(live_labels)
    grouped: dict[tuple, dict] = {}
    for d in doc_ids:
        for f in facts_by_doc.get(d, []):
            if f["label"] not in vlabels or not f["tok_span"]:
                continue
            missing_from_values = f["tok_span"] - doc_ops_value_toks[d]
            if not missing_from_values:
                continue                       # every token captured as a field VALUE
            missing_everywhere = missing_from_values - doc_ops_all_toks[d]
            tier = "strong" if missing_everywhere else "snippet_anchored"
            guess, method = guess_field(f["label"], f["param"], f["span"],
                                        exact_m, keyword_m)
            key = (d, f["label"], f["param"], frozenset(missing_from_values))
            slot = grouped.setdefault(key, {
                "doc_id": d, "label": f["label"], "param": f["param"],
                "tokens": sorted(missing_from_values),
                "tokens_absent_even_from_snippets": sorted(missing_everywhere),
                "tier": tier, "guessed_field": guess, "guess_method": method,
                "evidence": trunc(f["span"]), "n_facts": 0,
            })
            slot["n_facts"] += 1
    # view_mapping guesses first (a value that maps onto a DEFINED field is a true
    # extraction gap; keyword/none guesses may be schema-scope gaps instead).
    miss_candidates = sorted(grouped.values(),
                             key=lambda c: (0 if c["guess_method"] == "view_mapping" else 1,
                                            c["doc_id"], c["label"], c["param"],
                                            c["tokens"][0] if c["tokens"] else ""))
    miss_by_label: dict[str, int] = defaultdict(int)
    miss_by_method: dict[str, int] = defaultdict(int)
    miss_by_doc: dict[str, int] = defaultdict(int)
    for c in miss_candidates:
        miss_by_label[c["label"]] += 1
        miss_by_method["view_mapping" if c["guess_method"] == "view_mapping"
                       else ("keyword" if c["guessed_field"] else "no_guess")] += 1
        miss_by_doc[c["doc_id"]] += 1

    seeded = sorted((c for c in miss_candidates
                     if _SEEDED_ON
                     and c["doc_id"].startswith(_SEEDED_DOC_PREFIX)
                     and c["guessed_field"] and c["guessed_field"].endswith(_SEEDED_FIELD)),
                    key=lambda c: 0 if c["guess_method"] == "view_mapping" else 1)

    # ---- c. conflicts --------------------------------------------------------
    field_words = {k["full_key"]: k["words"] for k in keyword_m}
    conflicts = []
    for d in doc_ids:
        spans_by_page: dict[int, list[dict]] = defaultdict(list)
        for f in facts_by_doc.get(d, []):
            if f["page"] is not None:
                spans_by_page[int(f["page"])].append(f)
        for o in ops_by_doc[d]:
            if o["page"] is None or not o["tok_value"]:
                continue
            o_words = _words(o["snippet"])
            for f in spans_by_page.get(int(o["page"]), []):
                if not f["tok_span"]:
                    continue
                # same clause? — near-identical snippet text (containment / word overlap)
                f_words = _words(f["span"])
                inter = len(o_words & f_words)
                union = len(o_words | f_words) or 1
                snip_o = " ".join(str(o["snippet"]).lower().split())
                snip_f = " ".join(str(f["span"]).lower().split())
                same_clause = bool(snip_o) and bool(snip_f) and (
                    snip_o in snip_f or snip_f in snip_o or inter / union >= 0.5)
                if not same_clause:
                    continue
                if o["tok_value"] & f["tok_span"]:
                    continue                    # values agree on >=1 token — no conflict
                # Same QUANTITY, or merely co-located distinct facts in one clause?
                hint = bool(_words(f["param"].replace("_", " "), f["label"])
                            & field_words.get(o["full_key"], set()))
                conflicts.append({
                    "doc_id": d, "page": o["page"], "full_key": o["full_key"],
                    "ops_value": trunc(o["value"]), "ops_tokens": sorted(o["tok_value"]),
                    "fact_label": f["label"], "fact_param": f["param"],
                    "span_tokens": sorted(f["tok_span"]),
                    "same_quantity_hint": hint,
                    "ops_snippet": trunc(o["snippet"]), "fact_span": trunc(f["span"]),
                })
    conflicts.sort(key=lambda c: (not c["same_quantity_hint"], c["doc_id"],
                                  c["full_key"], c["fact_param"]))

    # ---- d. reverse integrity ------------------------------------------------
    reverse, partial = [], []
    for d in doc_ids:
        for o in ops_by_doc[d]:
            if not o["tok_value"]:
                continue
            missing = o["tok_value"] - doc_block_toks[d]
            if missing == o["tok_value"]:
                reverse.append({"doc_id": d, "full_key": o["full_key"],
                                "value": trunc(o["value"]), "tokens": sorted(missing),
                                "trust": o["trust"]})
            elif missing:
                partial.append({"doc_id": d, "full_key": o["full_key"],
                                "value": trunc(o["value"]),
                                "ungrounded_tokens": sorted(missing)})
    reverse.sort(key=lambda r: (r["doc_id"], r["full_key"]))
    partial.sort(key=lambda r: (r["doc_id"], r["full_key"]))

    # ---- e. sensitivity net audit --------------------------------------------
    # (i) confidential OpsField value tokens vs untagged blocks — RAW containment,
    # exactly the guard's own semantics (km.py::_VALUE_BLOCK_SENS), so a hit is a
    # real hole in the shipped net, not an artefact of our normalisation.
    leaks_ops, near_misses = [], []
    for d in doc_ids:
        raw_tokens: dict[str, list[str]] = defaultdict(list)   # raw tok -> fields
        for o in ops_by_doc[d]:
            if o["sensitivity"] != "confidential":
                continue
            vals = list(o["values"] or []) or [o["value"]]
            for v in vals:
                for t in distinctive_value_tokens(str(v or "")):
                    raw_tokens[t].append(o["full_key"])
        if not raw_tokens:
            continue
        norm_map = {t: norm_token(t) for t in raw_tokens}
        for b in blocks_by_doc.get(d, []):
            if b["sensitivity"] == "confidential":
                continue
            for t, fields in raw_tokens.items():
                if t in b["text"]:
                    leaks_ops.append({
                        "doc_id": d, "block_id": b["block_id"], "page": b["page"],
                        "token": t, "fields": sorted(set(fields)),
                        "block_text": trunc(b["text"]),
                    })
                elif norm_map[t] in b["tok"]:
                    near_misses.append({
                        "doc_id": d, "block_id": b["block_id"], "page": b["page"],
                        "token": t, "fields": sorted(set(fields)),
                        "block_text": trunc(b["text"]),
                    })

    # (ii) restricted-class LEGACY fact span tokens vs untagged blocks.
    fin_labels, _fin_raws = _policy_financials()
    leaks_legacy = []
    for d in doc_ids:
        span_tokens: dict[str, list[str]] = defaultdict(list)  # raw tok -> params
        for f in facts_by_doc.get(d, []):
            if f["label"] not in fin_labels:
                continue
            for t in distinctive_value_tokens(f["span"]):
                span_tokens[t].append(f"{f['label']}:{f['param']}")
        if not span_tokens:
            continue
        for b in blocks_by_doc.get(d, []):
            if b["sensitivity"] == "confidential":
                continue
            for t, sources in span_tokens.items():
                if t in b["text"]:
                    leaks_legacy.append({
                        "doc_id": d, "block_id": b["block_id"], "page": b["page"],
                        "token": t, "sources": sorted(set(sources)),
                        "block_text": trunc(b["text"]),
                    })

    # ---- f. family/DAG agreement ---------------------------------------------
    legacy_edges = {(e["src"], e["rel"], e["tgt"]) for e in data["legacy_edges"]}
    km_edges = {(e["src"], e["rel"], e["tgt"]) for e in data["km_edges"]}
    chained_legacy = {x for e in legacy_edges for x in (e[0], e[2])}
    chained_km = {x for e in km_edges for x in (e[0], e[2])}

    # What the ONTOLOGY layer says each doc declares. Both the field name and
    # the words it may contain are domain vocabulary, so they come from the
    # active view. Hardcoding either made this audit report a clean chain on
    # exactly the domains where the chain was broken.
    from pipeline.kb.km import rel_edge
    from pipeline.kb.opsview_spec import rel_full_key
    rel_key = rel_full_key("relationship_type")
    declared = {}
    for d in doc_ids:
        for o in ops_by_doc[d]:
            if o["full_key"] == rel_key:
                edge = rel_edge(str(o["value"]))
                if edge:
                    declared[d] = edge
    declared_unlinked_km = sorted(d for d in declared if d not in {e[0] for e in km_edges})
    declared_unlinked_legacy = sorted(d for d in declared if d not in {e[0] for e in legacy_edges})

    dag = {
        "legacy_edges": sorted(legacy_edges),
        "km_edges": sorted(km_edges),
        "edges_only_in_legacy": sorted(legacy_edges - km_edges),
        "edges_only_in_km": sorted(km_edges - legacy_edges),
        "docs_chained_legacy_only": sorted(chained_legacy - chained_km),
        "docs_chained_km_only": sorted(chained_km - chained_legacy),
        "ontology_declared_edge": {d: declared[d] for d in sorted(declared)},
        "declared_but_no_km_edge": declared_unlinked_km,
        "declared_but_no_legacy_edge": declared_unlinked_legacy,
        "pending_proposals": data["proposals"],
    }

    # ---- g. trust census -------------------------------------------------------
    trust = []
    for c in coverage:
        n = c["fields_filled"]
        trust.append({"doc_id": c["doc_id"], "filled": n,
                      "human_validated": c["human_validated"],
                      "pct_human": round(100.0 * c["human_validated"] / n, 1) if n else 0.0})
    tot_filled = sum(t["filled"] for t in trust)
    tot_human = sum(t["human_validated"] for t in trust)

    # ---- h. corroboration 2x2 ---------------------------------------------------
    cells = {"both": 0, "legacy_only": 0, "block_only": 0, "neither": 0}
    cell_fields: dict[str, list[str]] = {"legacy_only": [], "block_only": [], "neither": []}
    n_tokened = 0
    for d in doc_ids:
        for o in ops_by_doc[d]:
            if not o["tok_value"]:
                continue
            n_tokened += 1
            lg = bool(o["tok_value"] & doc_fact_toks[d])
            bl = bool(o["tok_value"] & doc_block_toks[d])
            cell = ("both" if lg and bl else
                    "legacy_only" if lg else
                    "block_only" if bl else "neither")
            cells[cell] += 1
            if cell != "both":
                cell_fields[cell].append(o["ops_id"])
    corro_rate = ((cells["both"] + cells["legacy_only"] + cells["block_only"]) / n_tokened
                  if n_tokened else 0.0)
    legacy_rate = ((cells["both"] + cells["legacy_only"]) / n_tokened) if n_tokened else 0.0

    return {
        "meta": {
            "run_date": date.today().isoformat(),
            "cosmos_uri": cfg.cosmos_uri,
            "n_docs": len(doc_ids),
            "n_ops_fields": len(data["ops"]),
            "n_fact_evidence_rows": len(data["facts"]),
            "n_blocks": len(data["blocks"]),
            "view_fields_total": len(view_keys),
            "value_bearing_labels": vlabels,
            "confidential_view_fields": len(conf_fields),
            "restricted_legacy_labels": sorted(fin_labels),
        },
        "a_coverage": coverage,
        "b_miss_candidates": miss_candidates,
        "b_summary": {"by_label": dict(sorted(miss_by_label.items())),
                      "by_guess_method": dict(sorted(miss_by_method.items())),
                      "by_doc": dict(sorted(miss_by_doc.items()))},
        "b_seeded_case": {"caught": bool(seeded), "matches": seeded},
        "c_conflicts": conflicts,
        "d_reverse_integrity": {"full_misses": reverse,
                                "partially_ungrounded": partial,
                                "n_partially_ungrounded": len(partial)},
        "e_sensitivity": {
            "ops_value_leaks": leaks_ops,
            "ops_value_near_misses": near_misses,
            "legacy_span_leaks": leaks_legacy,
        },
        "f_dag": dag,
        "g_trust": {"per_doc": trust, "total_filled": tot_filled,
                    "total_human_validated": tot_human,
                    "pct_human": round(100.0 * tot_human / tot_filled, 1) if tot_filled else 0.0},
        "h_corroboration": {
            "n_fields_with_tokens": n_tokened, "cells": cells,
            "non_both_cell_fields": cell_fields,
            "legacy_only_value_candidates_from_b": len(miss_candidates),
            "corroboration_rate": round(corro_rate, 4),
            "legacy_corroboration_rate": round(legacy_rate, 4),
        },
    }


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #
def _ex(items: list[dict], n: int = 5) -> list[dict]:
    return items[:n]


def render_md(r: dict) -> str:
    m, cor, tr = r["meta"], r["h_corroboration"], r["g_trust"]
    cells = cor["cells"]
    e = r["e_sensitivity"]
    dag = r["f_dag"]
    seeded = r["b_seeded_case"]
    avg_filled = (sum(c["fields_filled"] for c in r["a_coverage"]) / m["n_docs"]
                  if m["n_docs"] else 0)

    L: list[str] = []
    A = L.append
    A(f"# KB Reconciliation Audit — {m['run_date']}")
    A("")
    A(f"Read-only audit of `{m['cosmos_uri']}` — {m['n_docs']} documents, "
      f"{m['n_ops_fields']} OpsFields (ontology), {m['n_fact_evidence_rows']} legacy "
      f"fact-evidence rows, {m['n_blocks']} raw blocks. "
      f"Ontology schema: {m['view_fields_total']} fields.")
    A("")
    A("## Headline")
    A("")
    A(f"- **Coverage**: avg **{avg_filled:.1f} / {m['view_fields_total']}** fields filled "
      f"per doc ({100 * avg_filled / m['view_fields_total']:.0f}%).")
    A(f"- **Corroboration** ({cor['n_fields_with_tokens']} filled fields carry distinctive "
      f"value tokens): **{100 * cor['corroboration_rate']:.1f}%** corroborated by legacy "
      f"facts and/or raw text; {100 * cor['legacy_corroboration_rate']:.1f}% by the legacy "
      f"layer specifically.")
    A("")
    A("  | | legacy: yes | legacy: no |")
    A("  |---|---|---|")
    A(f"  | **block: yes** | both = {cells['both']} | ontology-only (block-grounded) = "
      f"{cells['block_only']} |")
    A(f"  | **block: no** | legacy-only-anchored = {cells['legacy_only']} | neither = "
      f"{cells['neither']} |")
    A("")
    A(f"  Plus **{cor['legacy_only_value_candidates_from_b']} legacy-only value "
      f"candidates** (section B) — values the legacy layer holds that no ontology field "
      f"captured.")
    A(f"- **Ontology-miss candidates (B)**: **{len(r['b_miss_candidates'])}** "
      f"(strong = {sum(1 for c in r['b_miss_candidates'] if c['tier'] == 'strong')}, "
      f"snippet-anchored = "
      f"{sum(1 for c in r['b_miss_candidates'] if c['tier'] == 'snippet_anchored')}). "
      + (f"Seeded regression `{_SEEDED_DOC_PREFIX}* → {_SEEDED_FIELD}`: "
         f"{'**CAUGHT ✓**' if seeded['caught'] else '**NOT CAUGHT — matcher broken**'}."
         if _SEEDED_ON else
         "No seeded regression configured (set RECONCILE_SEED_DOC and "
         "RECONCILE_SEED_FIELD to self-test the matcher)."))
    A(f"- **Conflicts (C)**: **{len(r['c_conflicts'])}** same-clause value disagreements.")
    A(f"- **Reverse integrity (D)**: **{len(r['d_reverse_integrity']['full_misses'])}** "
      f"OpsField values with NO token in raw text "
      f"({r['d_reverse_integrity']['n_partially_ungrounded']} partially ungrounded).")
    A(f"- **Sensitivity net (E)**: (i) confidential OpsField values in untagged blocks: "
      f"**{len(e['ops_value_leaks'])}** "
      f"{'✓ (expected 0)' if not e['ops_value_leaks'] else '— **P0 LEAK**'}; "
      f"(ii) restricted legacy-fact values in untagged blocks: "
      f"**{len(e['legacy_span_leaks'])}** "
      f"{'✓ (expected 0)' if not e['legacy_span_leaks'] else '— **P0 LEAK**'}. "
      f"Format-variant near-misses: {len(e['ops_value_near_misses'])}.")
    A(f"- **Family/DAG (F)**: legacy timeline {len(dag['legacy_edges'])} edges vs KM DAG "
      f"{len(dag['km_edges'])} edges; edges only-in-legacy = "
      f"{len(dag['edges_only_in_legacy'])}, only-in-KM = {len(dag['edges_only_in_km'])}; "
      f"docs declaring a relationship in the ontology with no KM edge = "
      f"{len(dag['declared_but_no_km_edge'])}.")
    A(f"- **Trust (G)**: {tr['total_human_validated']} / {tr['total_filled']} filled fields "
      f"human-validated (**{tr['pct_human']}%**).")
    A("")

    A("## A. Coverage census")
    A("")
    A("| doc | group | filled | absent | human | machine | legacy facts | blocks | sections |")
    A("|---|---|---|---|---|---|---|---|---|")
    for c in r["a_coverage"]:
        nfacts = sum(c["legacy_facts_by_label"].values())
        A(f"| `{c['doc_id'][:8]}` {trunc(c['title'], 34)} | {c['group'] or '—'} "
          f"| {c['fields_filled']}/{c['fields_total']} | {c['n_absent']} "
          f"| {c['human_validated']} | {c['machine_extracted']} | {nfacts} "
          f"| {c['n_blocks']} | {c['n_sections']} |")
    A("")
    A("Value-bearing legacy labels used for miss detection: "
      + ", ".join(f"`{l}`" for l in m["value_bearing_labels"]) + ".")
    A("")

    A("## B. Ontology-miss candidates (legacy values absent from ontology fields)")
    A("")
    A("A candidate = a legacy fact whose evidence carries distinctive value tokens that "
      "appear in NO OpsField **value** of the same document. `strong` = the tokens are "
      "absent from OpsField snippets too; `snippet_anchored` = some snippet quotes the "
      "clause but no field captured the value. Guess method `view_mapping` = the fact's "
      "normalized_parameter matches a defined field's own source mapping — a true "
      "extraction gap on an existing field; `keyword`/`no_guess` candidates may instead "
      "be values the 58-field schema never targets (schema-scope gaps).")
    A("")
    bs = r["b_summary"]
    A("By guess method: "
      + ", ".join(f"**{k}** = {v}" for k, v in bs["by_guess_method"].items()) + ".")
    A("")
    A("| legacy label | candidates |  | doc | candidates |")
    A("|---|---|---|---|---|")
    lbl_rows = sorted(bs["by_label"].items(), key=lambda x: -x[1])
    doc_rows = sorted(bs["by_doc"].items(), key=lambda x: -x[1])
    for i in range(max(len(lbl_rows), len(doc_rows))):
        l = f"`{lbl_rows[i][0]}` | {lbl_rows[i][1]}" if i < len(lbl_rows) else " | "
        d = f"`{doc_rows[i][0][:8]}` | {doc_rows[i][1]}" if i < len(doc_rows) else " | "
        A(f"| {l} |  | {d} |")
    A("")
    if doc_rows and r["b_miss_candidates"]:
        top_doc, top_n = doc_rows[0]
        share = 100 * top_n / len(r["b_miss_candidates"])
        if share >= 40:
            A(f"Concentration: `{top_doc[:8]}` alone contributes {top_n} of "
              f"{len(r['b_miss_candidates'])} candidates ({share:.0f}%) — inspect that "
              f"document's dominant label before treating the corpus-wide count as "
              f"an extraction-quality signal.")
            A("")
    if seeded["caught"]:
        s0 = seeded["matches"][0]
        A(f"**Seeded regression CAUGHT ✓** — `{s0['doc_id'][:8]}` [{s0['label']}] "
          f"`{s0['param']}` tokens {s0['tokens']} → guessed `{s0['guessed_field']}` "
          f"({s0['guess_method']}): “{s0['evidence']}”")
        A("")
    for c in _ex(r["b_miss_candidates"], 30):
        A(f"- `{c['doc_id'][:8]}` [{c['label']}] `{c['param'] or '—'}` ({c['tier']}, "
          f"×{c['n_facts']}) tokens **{', '.join(c['tokens'])}** → guess "
          f"`{c['guessed_field'] or '?'}` ({c['guess_method']})  \n  “{c['evidence']}”")
    if len(r["b_miss_candidates"]) > 30:
        A(f"- … {len(r['b_miss_candidates']) - 30} more in reconcile.json")
    A("")

    A("## C. Conflicts (same clause, different values)")
    A("")
    n_hint = sum(1 for c in r["c_conflicts"] if c["same_quantity_hint"])
    if not r["c_conflicts"]:
        A("None found — no OpsField and legacy fact cite the same clause with disjoint "
          "distinctive value tokens.")
    else:
        A(f"{len(r['c_conflicts'])} same-clause disagreements; **{n_hint}** carry a "
          f"same-quantity hint (the fact's parameter names the same concept as the "
          f"field). Hint-less pairs are often two DIFFERENT facts co-located in one "
          f"clause — human judges.")
        A("")
    for c in _ex(r["c_conflicts"], 8):
        tag = "same-quantity" if c["same_quantity_hint"] else "co-located"
        A(f"- [{tag}] `{c['doc_id'][:8]}` p{c['page']} `{c['full_key']}` = "
          f"“{c['ops_value']}” (tokens {c['ops_tokens']}) vs [{c['fact_label']}] "
          f"`{c['fact_param']}` (tokens {c['span_tokens']})  \n  ops: “{c['ops_snippet']}”"
          f"  \n  legacy: “{c['fact_span']}”")
    A("")

    A("## D. Reverse evidence integrity (ontology values with no raw-text grounding)")
    A("")
    fm = r["d_reverse_integrity"]["full_misses"]
    if not fm:
        A("None — every OpsField value token is grounded somewhere in its document's raw "
          "text. ✓")
    for x in _ex(fm, 8):
        A(f"- `{x['doc_id'][:8]}` `{x['full_key']}` ({x['trust']}) = “{x['value']}” — "
          f"tokens {x['tokens']} found in NO block of the doc")
    A(f"\nPartially ungrounded (some but not all tokens grounded): "
      f"{r['d_reverse_integrity']['n_partially_ungrounded']} fields.")
    for x in _ex(r["d_reverse_integrity"]["partially_ungrounded"], 5):
        A(f"- `{x['doc_id'][:8]}` `{x['full_key']}` = “{x['value']}” — ungrounded tokens "
          f"{x['ungrounded_tokens']}")
    A("")

    A("## E. Sensitivity net audit")
    A("")
    A(f"(i) Confidential OpsField value tokens in blocks NOT tagged "
      f"`sensitivity='confidential'` (guard semantics — raw containment): "
      f"**{len(e['ops_value_leaks'])} hits**"
      + (" ✓" if not e["ops_value_leaks"] else " — **P0**"))
    for x in _ex(e["ops_value_leaks"], 8):
        A(f"- `{x['doc_id'][:8]}` block `{x['block_id']}` p{x['page']} token "
          f"**{x['token']}** (fields: {', '.join(x['fields'])}) — “{x['block_text']}”")
    A("")
    A(f"(ii) Restricted-class legacy fact ({', '.join(m['restricted_legacy_labels'])}) "
      f"span tokens in untagged blocks: **{len(e['legacy_span_leaks'])} hits**"
      + (" ✓" if not e["legacy_span_leaks"] else " — **P0**"))
    for x in _ex(e["legacy_span_leaks"], 8):
        A(f"- `{x['doc_id'][:8]}` block `{x['block_id']}` p{x['page']} token "
          f"**{x['token']}** (from {', '.join(x['sources'][:3])}) — “{x['block_text']}”")
    A("")
    if e["ops_value_near_misses"]:
        A(f"Format-variant near-misses (the NUMBER matches an untagged block but the raw "
          f"string differs, so the shipped raw-containment guard cannot see it — often a "
          f"numeric coincidence, e.g. S$700,000 cap vs 700,000 RTh usage; verify by eye): "
          f"**{len(e['ops_value_near_misses'])}**")
        for x in _ex(e["ops_value_near_misses"], 5):
            A(f"- `{x['doc_id'][:8]}` block `{x['block_id']}` p{x['page']} token "
              f"**{x['token']}** (fields: {', '.join(x['fields'])}) — “{x['block_text']}”")
        A("")

    A("## F. Family/DAG agreement")
    A("")
    A(f"- Legacy timeline (Agreement-level): {len(dag['legacy_edges'])} edges")
    for s, rel, t in dag["legacy_edges"]:
        A(f"  - `{s[:8]}` —{rel}→ `{t[:8]}`")
    A(f"- KM DAG (Document-level): {len(dag['km_edges'])} edges")
    for s, rel, t in dag["km_edges"]:
        A(f"  - `{s[:8]}` —{rel}→ `{t[:8]}`")
    A(f"- Edges only in legacy: {len(dag['edges_only_in_legacy'])}; only in KM: "
      f"{len(dag['edges_only_in_km'])}")
    A("- Docs chained by legacy but orphaned by KM: "
      + (", ".join(f"`{d[:8]}`" for d in dag["docs_chained_legacy_only"]) or "none"))
    A("- Docs chained by KM but orphaned by legacy: "
      + (", ".join(f"`{d[:8]}`" for d in dag["docs_chained_km_only"]) or "none"))
    A("- Ontology-declared relationships: "
      + (", ".join(f"`{d[:8]}`→{rel}" for d, rel in dag["ontology_declared_edge"].items())
         or "none"))
    A("- Declared in ontology but NO KM edge materialised: "
      + (", ".join(f"`{d[:8]}`" for d in dag["declared_but_no_km_edge"]) or "none"))
    A("- Declared in ontology but NO legacy edge: "
      + (", ".join(f"`{d[:8]}`" for d in dag["declared_but_no_legacy_edge"]) or "none"))
    if dag["pending_proposals"]:
        A("- Pending supersedence proposals (legacy gate abstained):")
        for p in dag["pending_proposals"]:
            A(f"  - `{p['doc_id'][:8]}` {p['relation']} ({p['status']}): "
              f"{trunc(p['reason'], 100)}")
    A("")

    A("## G. Trust census")
    A("")
    A("| doc | filled | human-validated | % |")
    A("|---|---|---|---|")
    for t in tr["per_doc"]:
        A(f"| `{t['doc_id'][:8]}` | {t['filled']} | {t['human_validated']} "
          f"| {t['pct_human']}% |")
    A(f"\n**Corpus: {tr['total_human_validated']} / {tr['total_filled']} "
      f"({tr['pct_human']}%) human-validated.**")
    A("")

    A("## H. Corroboration detail")
    A("")
    A(f"Of {cor['n_fields_with_tokens']} filled OpsFields carrying distinctive value "
      f"tokens: both layers corroborate {cells['both']}, raw-text-only "
      f"{cells['block_only']}, legacy-only {cells['legacy_only']}, neither "
      f"{cells['neither']}. Overall corroboration rate "
      f"**{100 * cor['corroboration_rate']:.1f}%**. 'Neither' fields are exactly the "
      f"reverse-integrity hits in section D. The "
      f"{cor['legacy_only_value_candidates_from_b']} legacy-only value candidates "
      f"(section B) are the fourth quadrant — values only the legacy layer holds.")
    ncf = cor["non_both_cell_fields"]
    for cell in ("block_only", "legacy_only", "neither"):
        if ncf.get(cell):
            A(f"- {cell}: " + ", ".join(f"`{x}`" for x in ncf[cell][:6])
              + (" …" if len(ncf[cell]) > 6 else ""))
    A("")
    return "\n".join(L)


def print_headline(r: dict) -> None:
    m, cor, tr = r["meta"], r["h_corroboration"], r["g_trust"]
    e, dag = r["e_sensitivity"], r["f_dag"]
    cells = cor["cells"]
    avg = (sum(c["fields_filled"] for c in r["a_coverage"]) / m["n_docs"]) if m["n_docs"] else 0
    print(f"\n[RECONCILE] {m['n_docs']} docs | {m['n_ops_fields']} OpsFields | "
          f"{m['n_fact_evidence_rows']} fact-evidence rows | {m['n_blocks']} blocks")
    print(f"  coverage        avg {avg:.1f}/{m['view_fields_total']} fields filled "
          f"({100 * avg / m['view_fields_total']:.0f}%)")
    print(f"  corroboration   {100 * cor['corroboration_rate']:.1f}% of "
          f"{cor['n_fields_with_tokens']} tokened fields "
          f"(both={cells['both']} block-only={cells['block_only']} "
          f"legacy-only={cells['legacy_only']} neither={cells['neither']})")
    print(f"  miss candidates {len(r['b_miss_candidates'])} "
          f"(strong={sum(1 for c in r['b_miss_candidates'] if c['tier'] == 'strong')}) | "
          + (f"seeded {_SEEDED_DOC_PREFIX}*/{_SEEDED_FIELD}: "
             f"{'CAUGHT' if r['b_seeded_case']['caught'] else 'NOT CAUGHT'}"
             if _SEEDED_ON else "no seeded regression configured"))
    print(f"  conflicts       {len(r['c_conflicts'])}")
    print(f"  reverse integ.  {len(r['d_reverse_integrity']['full_misses'])} full misses, "
          f"{r['d_reverse_integrity']['n_partially_ungrounded']} partial")
    print(f"  sensitivity     ops-leaks={len(e['ops_value_leaks'])} "
          f"legacy-leaks={len(e['legacy_span_leaks'])} "
          f"near-misses={len(e['ops_value_near_misses'])}")
    print(f"  DAG             legacy={len(dag['legacy_edges'])} km={len(dag['km_edges'])} "
          f"declared-but-no-km-edge={len(dag['declared_but_no_km_edge'])}")
    print(f"  trust           {tr['total_human_validated']}/{tr['total_filled']} "
          f"({tr['pct_human']}%) human-validated")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only KB reconciliation audit.")
    ap.add_argument("--out", default=str(OUT_DIR), help="output directory")
    ap.add_argument("--live", action="store_true",
                    help="allow auditing a cloud Cosmos account (read-only)")
    args = ap.parse_args()

    _assert_read_only()
    cfg = Config.load()
    _assert_local(cfg, allow_live=args.live)

    result = run_audit(cfg)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "reconcile.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "reconcile_report.md").write_text(render_md(result), encoding="utf-8")

    print_headline(result)
    print(f"\n  report: {out / 'reconcile_report.md'}\n  json:   {out / 'reconcile.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
