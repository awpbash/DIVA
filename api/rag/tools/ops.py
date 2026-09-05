"""The ALIGNED / VERIFIED knowledge base tool: the schema-first,
human-reviewed opsfield surface (lookup_verified_fields,
aggregate_ops_fields) and the pinned-family scope they both admit against.

Queries opsfield items — the per-document ops-view schema fields the ingestion
+ review pipeline produces (LLM fills the named field with evidence; a human
verifies; kb/km.py aligns them with per-field supersedence + trust). This is
the AUTHORITATIVE surface for "what is <named field>" questions: values are
apples-to-apples across documents, stale statements are excluded (is_current),
and every value carries its trust tier so the answer can say whether a human
stood behind it.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from pipeline.store.aio import AsyncCosmosStore
from pipeline.store.query import keyword_score

from ..schemas import Citation
from ._shared import _fetch_by_ids, _review_consensus, _scope_sql

# The chat layer's viewer roles (general / finance) predate the account roles
# (default / confidential / admin). Map them onto the ontology's sensitivity
# model so a confidential field never reaches a general viewer AT THE SOURCE.
_CHAT_ROLE_SEES_CONFIDENTIAL = {"finance", "admin", "confidential"}


def _ops_visible_item(o: dict, role: str | None) -> bool:
    if (role or "").lower() in _CHAT_ROLE_SEES_CONFIDENTIAL:
        return True
    return (o.get("sensitivity") or "general") != "confidential"


# Qualifier / filler words stripped from the query before token matching —
# they describe HOW to answer ("current", "latest"), not WHICH field.
_OPS_STOPWORDS = {
    "the", "a", "an", "of", "for", "in", "on", "to", "and", "or", "is", "are",
    "what", "whats", "which", "who", "how", "much", "many", "per", "under",
    "current", "latest", "most", "recent", "now", "today", "contract", "agreement",
}


def _ops_row(o: dict, d: dict | None) -> dict:
    return {
        "ops_id": o.get("ops_id") or o["id"], "doc_id": o["doc_id"],
        "title": o.get("title"), "category": o.get("category"),
        "value": o.get("value"), "unit": o.get("unit"),
        "trust": o.get("trust"), "verified": o.get("verified"),
        "confidence": o.get("confidence"), "verifiers": o.get("verifiers"),
        "n_votes": o.get("n_votes"), "disputed": bool(o.get("disputed")),
        "is_current": o.get("is_current", True) is not False,
        "superseded_by": o.get("superseded_by"),
        "snippet": o.get("snippet"), "page": o.get("page"),
        "rects": o.get("rects"),
        "doc_title": (d or {}).get("title") or o["doc_id"],
        "eff": (d or {}).get("effective_date") or "",
        "numbers": o.get("numbers"),
        "carried_from": o.get("carried_from"),
    }


def _ops_row_to_citation(r: dict) -> Citation:
    rects = None
    try:
        parsed = json.loads(r["rects"]) if r.get("rects") else None
        if isinstance(parsed, list) and parsed:
            rects = parsed
    except (json.JSONDecodeError, TypeError):
        pass
    if r.get("trust") == "human_validated":
        trust_tag = "HUMAN-VERIFIED ✓"
        conf, n_votes = r.get("confidence"), int(r.get("n_votes") or 0)
        if conf and n_votes:
            approvers = len(r.get("verifiers") or []) or round(float(conf) * n_votes)
            trust_tag += f" {round(float(conf) * 100)}% ({approvers}/{n_votes} verifiers)"
    elif r.get("disputed"):
        trust_tag = "DISPUTED: verifiers disagree on this value"
    else:
        trust_tag = "AI-extracted, unreviewed"
    currency = "" if r.get("is_current") else " · SUPERSEDED by a later document"
    summary = (f"[{trust_tag}{currency}] {r['title']} = {r['value']}"
               f" ({str(r.get('category') or '').replace('_', ' ')})")
    if r.get("carried_from"):
        summary += (f" · carried over from {r['carried_from']} "
                    f"(unchanged by the pinned document)")
    return Citation(
        evidence_id=f"ops:{r['ops_id']}",
        citation_kind="evidence_span",
        confidence_tier="canonical",
        doc_id=r["doc_id"],
        page_no=int(r["page"] or 1),
        snippet=(r.get("snippet") or str(r.get("value") or ""))[:300],
        bbox=(rects[0]["bbox"] if rects else None),
        rects=rects,
        fact_label="OpsField",
        fact_id=r["ops_id"],
        fact_summary=summary,
        score=0.97 if r.get("verified") else 0.93,
        field_value=(str(r["value"]) if r.get("value") not in (None, "") else None),
        **_review_consensus(r),
    )


async def build_ops_scope(store: AsyncCosmosStore,
                          pinned_doc_ids: list[str] | None) -> dict | None:
    """OPS-layer family expansion for a USER-pinned document set, computed
    ONCE per chat request (chat.py), not per tool call and not per row.

    A pinned amendment inherits: a field the amendment does not re-state
    lives on another family document as its is_current row, so the ops tools
    admit CURRENT rows from unpinned family siblings, marked carried-over.
    Raw-text and fact tools stay strictly pinned (they carry per-clause text,
    not per-field currency, so expansion there would leak sibling clauses)."""
    if not pinned_doc_ids:
        return None
    docs = await store.query(
        "SELECT c.doc_id, c.title, c['group'] AS grp FROM c "
        "WHERE c.kind = 'document'")
    pinned = list(pinned_doc_ids)
    pinned_set = set(pinned)
    groups = {d.get("grp") for d in docs
              if d["doc_id"] in pinned_set and d.get("grp")}
    expanded = list(pinned)
    for d in docs:
        if d.get("grp") in groups and d["doc_id"] not in pinned_set:
            expanded.append(d["doc_id"])
    return {
        "pinned": pinned,
        "doc_ids": expanded,
        "titles": {d["doc_id"]: d.get("title") or d["doc_id"] for d in docs},
    }


def _ops_admit(cands: list[dict], ops_scope: dict | None) -> list[dict]:
    """Pinned-scope admission for OPS rows. Pinned docs keep every candidate
    row. Unpinned family siblings contribute ONLY is_current rows, marked so
    the serialized output says the value carried over unchanged. Aggregates
    over the admitted set are correct by construction (current-only)."""
    if not ops_scope:
        return cands
    pinned = set(ops_scope["pinned"])
    titles = ops_scope.get("titles") or {}
    out: list[dict] = []
    for o in cands:
        if o["doc_id"] in pinned:
            out.append(o)
        elif o.get("is_current", True) is not False:
            out.append({**o,
                        "carried_from": titles.get(o["doc_id"]) or o["doc_id"]})
    return out


async def _ops_candidates(store: AsyncCosmosStore, doc_ids: list[str] | None,
                          include_superseded: bool, role: str | None) -> list[dict]:
    params: list[dict] = []
    rows = await store.query(
        f"SELECT * FROM c WHERE c.kind = 'opsfield' AND {_scope_sql(doc_ids, params)}",
        params)
    out = []
    for o in rows:
        if not include_superseded and o.get("is_current", True) is False:
            continue
        if not _ops_visible_item(o, role):
            continue
        out.append(o)
    return out


async def lookup_verified_fields(
    *, store: AsyncCosmosStore, query: str, role: str | None = None,
    doc_ids: list[str] | None = None,
    include_superseded: bool = False, k: int = 12,
    ops_scope: dict | None = None,
) -> list[Citation]:
    """Search the aligned knowledge base by field name / topic words. Ranked
    by the deterministic keyword score over title/value/snippet/key/category
    (the Lucene index's replacement), with the old token-majority floor:
    at least one real token must hit. Returns one citation per (document ×
    field) statement, current-only by default, trust-tagged
    ([HUMAN-VERIFIED] vs [AI-extracted]). With a pinned ``ops_scope`` the
    candidate set expands to the pinned docs' families, current-only and
    carried-over-marked for unpinned siblings."""
    tokens = [t for t in re.split(r"[^a-z0-9°$%.]+", (query or "").lower())
              if len(t) >= 2 and t not in _OPS_STOPWORDS]
    if not tokens:
        return []
    scope_ids = ops_scope["doc_ids"] if ops_scope else doc_ids
    cands = _ops_admit(
        await _ops_candidates(store, scope_ids, include_superseded, role),
        ops_scope)
    if not cands:
        return []
    docs = await _fetch_by_ids(store, sorted({o["doc_id"] for o in cands}),
                               kind="document")

    def _blob(o: dict, d: dict | None) -> str:
        return " ".join(str(x) for x in (
            o.get("title"), o.get("field_key"), o.get("category"),
            o.get("value"), o.get("snippet"), (d or {}).get("title")) if x)

    scored = []
    for o in cands:
        d = docs.get(o["doc_id"])
        blob = _blob(o, d).lower()
        matches = sum(1 for t in tokens if t in blob)
        if matches < 1:
            continue
        s = keyword_score(tokens, blob)
        # Trust is a ranking WEIGHT, not just a badge: a human-verified value
        # outranks a similarly-relevant machine read, and a disputed value
        # sinks below both. (Multiplicative — keyword relevance still leads,
        # so an off-topic verified field cannot bury the on-topic answer.)
        if o.get("trust") == "human_validated":
            s *= 1.3
        elif o.get("disputed"):
            s *= 0.75
        scored.append((s, matches, o, d))
    scored.sort(key=lambda t: (-t[0], -t[1],
                               not bool(t[2].get("verified")),
                               -(len((t[3] or {}).get("effective_date") or "")),
                               str(t[2].get("title") or "")))
    return [_ops_row_to_citation(_ops_row(o, d)) for _, _, o, d in scored[:k]]


_OPS_AGG_OPS = ("sum", "avg", "min", "max", "count", "list")


async def aggregate_ops_fields(
    *, store: AsyncCosmosStore, field: str, op: str = "sum",
    group: str | None = None, role: str | None = None,
    doc_ids: list[str] | None = None, ops_scope: dict | None = None,
) -> dict:
    """Deterministic arithmetic over the aligned knowledge base — sum / avg /
    min / max / count / list a field's numeric values across CURRENT statements
    (per contract family or corpus-wide). Exact math in code, never LLM
    estimation. ``field`` accepts the field key or plain words ("energy charge
    fees"); it resolves to the best-matching ontology field first.

    RBAC: confidential fields are excluded for uncleared viewers — the numbers
    AND the fact of them never enter the result."""
    if op not in _OPS_AGG_OPS:
        return {"error": f"unknown op {op!r}; use one of {_OPS_AGG_OPS}"}

    ftoks = [t for t in re.split(r"[^a-z0-9]+", (field or "").lower()) if len(t) >= 2]
    if not ftoks:
        return {"error": "field is required"}

    # 1. Resolve to ONE ontology field key (best token overlap wins).
    cands = await store.query(
        "SELECT DISTINCT c.field_key, c.title FROM c WHERE c.kind = 'opsfield'")
    best_fk, best_score = None, 0
    for c in cands:
        blob = f"{c['field_key']} {c.get('title') or ''}".lower() \
            .replace("_", " ").replace(".", " ")
        score = sum(1 for t in ftoks if t in blob)
        if score > best_score or (score == best_score and best_fk
                                  and c["field_key"] < best_fk):
            if score > 0:
                best_fk, best_score = c["field_key"], score
    if not best_fk:
        return {"error": f"no knowledge-base field matches {field!r}"}

    # 2. Fetch the CURRENT, role-visible statements of that field. A pinned
    # ops_scope expands to the families. Current-only holds for every row, so
    # the arithmetic over the expanded set is correct by construction.
    scope_ids = ops_scope["doc_ids"] if ops_scope else doc_ids
    params: list[dict] = [{"name": "@fk", "value": best_fk}]
    rows_raw = await store.query(
        f"SELECT * FROM c WHERE c.kind = 'opsfield' AND c.field_key = @fk "
        f"AND {_scope_sql(scope_ids, params)}", params)
    rows_raw = [o for o in rows_raw
                if o.get("is_current", True) is not False
                and _ops_visible_item(o, role)]
    rows_raw = _ops_admit(rows_raw, ops_scope)
    docs = await _fetch_by_ids(store, sorted({o["doc_id"] for o in rows_raw}),
                               kind="document")
    if group is not None:
        rows_raw = [o for o in rows_raw
                    if ((docs.get(o["doc_id"]) or {}).get("group")
                        or o["doc_id"]) == group]
    rows = [_ops_row(o, docs.get(o["doc_id"])) for o in rows_raw]
    rows.sort(key=lambda r: r.get("eff") or "", reverse=True)

    # Numbers are pooled PER UNIT, never across units. Summing a $/kWh rate
    # with a $/RTh rate produces a number that means nothing, and the old code
    # then stamped it with whichever unit happened to appear most often, so the
    # model received a confident wrong figure and put it in the answer.
    # `km_query.aggregate` already refuses this on the knowledge surface. Chat
    # is the surface people actually use, so it has to refuse it too.
    items, nums = [], []
    by_unit: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        row_nums = [float(n) for n in (r.get("numbers") or [])]
        nums.extend(row_nums)
        unit_key = str(r.get("unit") or "").strip() or "(none)"
        by_unit[unit_key].extend(row_nums)
        item = {"doc": r.get("doc_title") or r["doc_id"], "value": r.get("value"),
                "unit": r.get("unit"), "numbers": row_nums,
                "verified": bool(r.get("verified"))}
        if r.get("carried_from"):
            item["note"] = (f"carried over from {r['carried_from']} "
                            f"(unchanged by the pinned document)")
        items.append(item)

    out = {
        "field_key": best_fk, "op": op, "group": group,
        "n_numbers": len(nums), "n_docs": len(items),
        "items": items[:20],
        "sample": [_ops_row_to_citation(r) for r in rows[:5]],
    }
    mixed = len(by_unit) > 1
    # count is a pure tally, so it is the one operation that stays meaningful
    # when the units disagree.
    if mixed and op != "count":
        out["value"] = None
        out["unit"] = None
        out["by_unit"] = [
            {"unit": None if u == "(none)" else u,
             "value": round({"sum": sum(ns), "avg": sum(ns) / len(ns),
                             "min": min(ns), "max": max(ns),
                             "count": float(len(ns)), "list": None}[op], 4)
             if op != "list" and ns else None,
             "n_numbers": len(ns)}
            for u, ns in sorted(by_unit.items())
        ]
        out["note"] = ("values span multiple units, so there is no single "
                       "total. Report the per-unit figures in by_unit "
                       "separately, and never add them together.")
        return out

    value: float | None = None
    if nums:
        value = {"sum": sum(nums), "avg": sum(nums) / len(nums),
                 "min": min(nums), "max": max(nums),
                 "count": float(len(nums)), "list": None}[op]
        if value is not None:
            value = round(value, 4)
    only_unit = next(iter(by_unit)) if len(by_unit) == 1 else None
    out["value"] = value
    out["unit"] = None if only_unit in (None, "(none)") else only_unit
    return out
