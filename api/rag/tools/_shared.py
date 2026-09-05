"""Shared building blocks for the retrieval tools: row-to-Citation
conversion, confidentiality/visibility checks, Cosmos fetch helpers, and
the embedding call. Every other module in this package imports from here.

Port shape (Neo4j → Cosmos document model, pipeline/store/model.py):
  * the old Cypher projections become: candidate selection (vector RAM /
    SQL filter / Python keyword score) + one shared Python enrichment step
    (``_enrich_evidence``) that joins sections, parent facts and the derived
    fact-to-fact edge items;
  * the confidentiality guards become plain field checks —
    ``evidence.cites_confidential`` / ``mention.cites_confidential`` /
    ``block.sensitivity`` are stamped at KM-build time (the cross-layer
    value net), so no per-query join can miss them;
  * Lucene/BM25 → candidate fetch via case-insensitive CONTAINS + Python
    ``keyword_score`` (pipeline/store/query.py) — deterministic and exact at
    this corpus size;
  * aggregations fetch the matching rows and do the arithmetic in Python
    (exact, emulator-proof).
"""
from __future__ import annotations

import json
from collections import defaultdict
from functools import lru_cache

from openai import AsyncOpenAI

from pipeline.extraction.pack import load as load_pack
from pipeline.store.aio import AsyncCosmosStore

from ..schemas import Citation


# ---------------------------------------------------------------------------
# Shared row → Citation conversion
# ---------------------------------------------------------------------------


# Per-label retrieval metadata, read from the ACTIVE pack rather than written
# out here. The pack already declares a summary projection, an id key and a
# filterable allowlist for every fact type, and validates each against the
# label's declared properties. These were literal copies of one domain's
# answers, so on any other domain the labels they did not name lost their
# summary line, lost their stable id, and could not be filtered at all.


@lru_cache(maxsize=1)
def _fact_summary_keys() -> dict[str, tuple[str, ...]]:
    pack = load_pack()
    return {lab: tuple(pack.summary_keys(lab)) for lab in pack.fact_labels}


@lru_cache(maxsize=1)
def _id_key_by_label() -> dict[str, str]:
    pack = load_pack()
    return {lab: k for lab in pack.fact_labels if (k := pack.id_key(lab))}


# Column projections for the three EMBEDDING-BEARING kinds
# (pipeline/store/model.EMBEDDED_KINDS: evidence, section, block).
#
# Each of those rows carries a 3072-float vector, roughly 60KB serialised, and
# `SELECT *` drags it across the wire for fields nobody reads. Invisible on the
# local emulator, several megabytes per chat turn on live Azure.
#
# These lists are deliberately GENEROUS. A column named here that an item does
# not have simply comes back absent, which costs nothing. A column MISSING from
# here reads as None downstream, silently, which is the failure this codebase
# keeps having. When in doubt, add the column.
_EVIDENCE_COLS = (
    "id", "doc_id", "kind", "page_no", "section_id", "section_num",
    "section_title", "target_section_id", "agreement_id", "fact_id",
    "text_span", "snippet", "raw_label", "row_context", "rects_json",
    "bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1",
    "canonicalized", "confidence", "confidence_tier", "evidence_type",
    "cites_confidential", "sensitivity", "via",
    "linked_conditions", "linked_events", "linked_terms",
    "linked_computes", "linked_measures",
    # Present on stored evidence and read by the citation path. Verified
    # against a live row rather than assumed: a column left out here is a
    # silent None downstream, which is the whole failure mode.
    "evidence_id", "block_ids", "raw_fact_id", "snippet_ok",
)
_SECTION_COLS = (
    "id", "doc_id", "kind", "section_id", "section_num", "title", "text",
    "page_start", "page_no", "bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1",
    "sensitivity",
)
_BLOCK_COLS = (
    "id", "doc_id", "kind", "block_id", "section_id", "page_no", "text",
    "bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1", "sensitivity", "kind_hint",
)

# Cosmos rejects these as a bare property path AND as a column alias, so a
# projection cannot carry them under their own name. Nothing in this module
# reads them off a section or block row, so they are simply left out rather
# than aliased to something the consumers would then have to know about.
# `order` is the one that matters: it is why the section projection has no
# reading-order column. Same family as the `AS value` trap covered by tests.
_UNPROJECTABLE = {"order", "value", "group", "key", "end"}


def _cols(names: tuple[str, ...]) -> str:
    """`c.a, c.b, …` for a SELECT list, minus anything Cosmos cannot alias."""
    return ", ".join(f"c.{n}" for n in names if n not in _UNPROJECTABLE)


def _bbox(r: dict) -> list[float] | None:
    parts = [r.get("bbox_x0"), r.get("bbox_y0"), r.get("bbox_x1"), r.get("bbox_y1")]
    if any(p is None for p in parts):
        return None
    return [float(p) for p in parts]


def _fact_summary(label: str | None, props: dict | None) -> str | None:
    if not label or not props:
        return None
    keys = _fact_summary_keys().get(label)
    if not keys:
        return None
    parts: list[str] = []
    for k in keys:
        v = props.get(k)
        if v is None or v == "":
            continue
        parts.append(str(v))
    return " · ".join(parts) if parts else None


def _fact_id(label: str | None, props: dict | None) -> str | None:
    if not label or not props:
        return None
    key = _id_key_by_label().get(label)
    return props.get(key) if key else None


def _parse_rects(rects_json) -> list[dict] | None:
    """Per-page rects from the citation atom's JSON property; None when
    absent or unparseable (frontend falls back to the envelope bbox)."""
    if not rects_json:
        return None
    try:
        rects = json.loads(rects_json)
    except (json.JSONDecodeError, TypeError):
        return None
    return rects if isinstance(rects, list) and rects else None


def _infer_citation_kind(r: dict) -> str:
    """Best-effort citation kind for projections that don't set it."""
    kind = r.get("citation_kind")
    if kind:
        return str(kind)
    eid = str(r.get("evidence_id") or "")
    if ":m:" in eid:
        return "fact_mention"
    if ":block:" in eid:
        return "block"
    if eid.endswith(":heading"):
        return "section_heading"
    if r.get("evidence_type") == "inferred_reference":
        return "reference"
    return "evidence_span"


def _default_confidence_tier(kind: str) -> str:
    return {
        "evidence_span": "canonical",
        "reference": "reference",
        "fact_mention": "raw",
        "block": "layout",
        "section_heading": "layout",
    }.get(kind, "canonical")


def _linked_context(r: dict) -> str | None:
    """Flatten the linked_* collections into the prompt-ready block:
    one line per linked fact, prefixed by its relationship kind."""
    parts: list[str] = []
    parts += [f"IF: {t}" for t in (r.get("linked_conditions") or []) if t]
    parts += [f"TRIGGER: {t}" for t in (r.get("linked_events") or []) if t]
    parts += [f"TERM {t}" for t in (r.get("linked_terms") or []) if t]
    parts += [f"COMPUTES: {t}" for t in (r.get("linked_computes") or []) if t]
    parts += [f"MEASURES: {t}" for t in (r.get("linked_measures") or []) if t]
    return "\n".join(parts) if parts else None


# status → the tag the synth model reads. 'restated' is a value a LATER document
# re-states verbatim: still the value in force, so it must read as CURRENT (never
# "superseded by itself" — the 0.0241-superseded-0.0241 display bug).
_CURRENCY_TAGS = {
    "active": "CURRENT",
    "restated": "CURRENT (re-stated later; value unchanged)",
    "superseded": "superseded",
}


def _currency_prefixed(fact_props: dict | None, summary: str | None) -> str | None:
    """Prefix a summary with its supersedence currency so stale data is
    visible: superseded statements are tagged and synth prefers the active
    one. The tag is only present when the KM layer materialised it."""
    if not fact_props:
        return summary
    status, eff = fact_props.get("status"), fact_props.get("effective_date")
    tag = _CURRENCY_TAGS.get(status or "")
    if not tag or not eff:
        return summary
    return f"[{tag} · effective {eff}] " + (summary or "")


def _review_consensus(props: dict | None) -> dict:
    """Reviewer-confidence fields, read off whatever node carries the review
    stamps (an OpsField row, or a KM fact node: ``_propagate_to_km`` in
    review.py writes the same keys onto both: ``trust``/``verified``,
    ``confidence``, ``verifiers``, ``n_votes``, ``disputed``). One derivation
    so a citation shows the same "verified by N of M" badge everywhere it is
    backed by a reviewed fact, not only on the structured field-lookup tools
    that happened to read it first."""
    props = props or {}
    if props.get("trust") == "human_validated" or props.get("verified"):
        field_trust = "human_validated"
    elif props.get("disputed"):
        field_trust = "disputed"
    else:
        field_trust = None
    return {
        "field_trust": field_trust,
        "verified_by": (list(props.get("verifiers") or []) or None),
        "verify_confidence": (float(props["confidence"])
                              if props.get("confidence") is not None else None),
        "verify_votes": (int(props["n_votes"])
                         if props.get("n_votes") is not None else None),
    }


def _row_to_citation(r: dict, *, default_score: float = 0.0) -> Citation:
    fact_labels = r.get("fact_labels") or []
    fact_label = next(
        (lab for lab in fact_labels if lab in _id_key_by_label()),
        fact_labels[0] if fact_labels else None,
    )
    fact_props = dict(r["fact_props"]) if r.get("fact_props") else None
    citation_kind = _infer_citation_kind(r)
    return Citation(
        evidence_id=r["evidence_id"],
        citation_kind=citation_kind,
        canonicalized=(
            bool(r["canonicalized"]) if r.get("canonicalized") is not None
            else (False if citation_kind == "fact_mention" else None)
        ),
        confidence_tier=(
            r.get("confidence_tier") or _default_confidence_tier(citation_kind)
        ),
        doc_id=r["doc_id"],
        page_no=int(r.get("page_no") or 0),
        section_id=r.get("section_id"),
        section_num=r.get("section_num"),
        section_title=r.get("section_title"),
        snippet=r.get("snippet") or "",
        bbox=_bbox(r),
        rects=_parse_rects(r.get("rects_json")),
        fact_label=fact_label,
        fact_id=_fact_id(fact_label, fact_props),
        fact_summary=_currency_prefixed(fact_props, _fact_summary(fact_label, fact_props)),
        raw_label=r.get("raw_label"),
        row_context=r.get("row_context"),
        linked_context=_linked_context(r),
        score=float(r.get("score") if r.get("score") is not None else default_score),
        **_review_consensus(fact_props),
    )


def _verification_boost(c: Citation) -> float:
    """A nudge, not a gate: unreviewed evidence must stay fully citable (most
    of the corpus has no review yet, and a relevant unreviewed clause beats an
    irrelevant reviewed one), so this only tips close calls rather than
    overriding topical relevance. More reviewers moves it further, capped so
    a single vote isn't indistinguishable from a five-person consensus.
    Disputed evidence (verifiers actively disagree) gets the opposite nudge."""
    if c.field_trust == "human_validated":
        return 0.05 + 0.01 * min(c.verify_votes or 1, 5)
    if c.field_trust == "disputed":
        return -0.05
    return 0.0


def _rank_citations(citations: list[Citation], k: int | None = None) -> list[Citation]:
    """Dedupe by citation id, then rank by relevance with a verification
    nudge, then prefer stronger tiers at equal score. A `k` cutoff drops the
    lowest-priority items, so the nudge is what lets a well-reviewed clause
    survive a truncation an equally-relevant unreviewed one wouldn't."""
    tier_rank = {"canonical": 0, "reference": 1, "raw": 2, "layout": 3}
    by_id: dict[str, Citation] = {}
    for c in citations:
        if not c.evidence_id:
            continue
        existing = by_id.get(c.evidence_id)
        if existing is None or (
            c.score + _verification_boost(c),
            -tier_rank.get(c.confidence_tier or "", 9),
        ) > (
            existing.score + _verification_boost(existing),
            -tier_rank.get(existing.confidence_tier or "", 9),
        ):
            by_id[c.evidence_id] = c
    out = sorted(
        by_id.values(),
        key=lambda c: (
            -(c.score + _verification_boost(c)),
            tier_rank.get(c.confidence_tier or "", 9),
            c.page_no,
            c.evidence_id,
        ),
    )
    return out[:k] if k is not None else out


# ---------------------------------------------------------------------------
# Store fetch helpers (the shared enrichment that replaced the projections)
# ---------------------------------------------------------------------------


# Roles cleared for confidential text (mirror of the OpsField source filter).
_CLEARED_TEXT_ROLES = {"admin", "confidential", "finance"}


def _text_cleared(role: str | None) -> bool:
    return (role or "").lower() in _CLEARED_TEXT_ROLES


def _span_visible(item: dict, role: str | None) -> bool:
    """Evidence/mention-level confidentiality: a span citing a confidential
    block is invisible to uncleared roles. ``cites_confidential`` is stamped
    at KM-build time (the cross-layer value net)."""
    return _text_cleared(role) or not item.get("cites_confidential")


def _block_visible(item: dict, role: str | None) -> bool:
    return _text_cleared(role) or (item.get("sensitivity") or "general") != "confidential"


# Kinds that carry an embedding, and the columns to project instead of `*`.
# Fetching one of these without a projection pulls a 3072-float vector per row.
_EMBEDDED_COLS = {
    "evidence": _EVIDENCE_COLS,
    "section": _SECTION_COLS,
    "block": _BLOCK_COLS,
}


async def _fetch_by_ids(store: AsyncCosmosStore, ids: list[str],
                        kind: str | None = None) -> dict[str, dict]:
    """id -> item for a small id list (cross-partition IN fetch).

    Projects the embedding-bearing kinds. Three of the call sites below fetch
    evidence, section and block by id, so the default `SELECT *` was pulling a
    vector per row purely to read a snippet and a bounding box.
    """
    if not ids:
        return {}
    cols = _cols(_EMBEDDED_COLS[kind]) if kind in _EMBEDDED_COLS else "*"
    sql = f"SELECT {cols} FROM c WHERE ARRAY_CONTAINS(@ids, c.id)"
    params = [{"name": "@ids", "value": sorted(set(ids))}]
    if kind:
        sql += " AND c.kind = @kind"
        params.append({"name": "@kind", "value": kind})
    return {r["id"]: r for r in await store.query(sql, params)}


_VIA_RANK = {"same_block": 0, "cross_ref": 1, "same_section": 2}


async def _linked_context_fields(store: AsyncCosmosStore,
                                 fact_ids: list[str]) -> dict[str, dict]:
    """fact_id -> {linked_conditions, linked_events, linked_terms,
    linked_computes, linked_measures}. Walks the derived fact-to-fact edge
    items so a retrieved fact arrives WITH its condition / trigger /
    defined-term context — fetched from the store, not re-inferred by the
    LLM from whatever other chunks happened to make the retrieval cut."""
    if not fact_ids:
        return {}
    edges = await store.query(
        "SELECT c.src, c.tgt, c.rel, c.via FROM c WHERE c.kind = 'edge' "
        "AND ARRAY_CONTAINS(@srcs, c.src) AND ARRAY_CONTAINS(@rels, c.rel)",
        [{"name": "@srcs", "value": sorted(set(fact_ids))},
         {"name": "@rels", "value": ["CONDITIONED_ON", "TRIGGERED_BY",
                                     "USES_TERM", "COMPUTES", "MEASURES"]}])
    if not edges:
        return {}
    targets = await _fetch_by_ids(store, [e["tgt"] for e in edges], kind="fact")

    by_fact: dict[str, dict[str, list[tuple[int, str]]]] = defaultdict(
        lambda: defaultdict(list))
    for e in edges:
        t = targets.get(e["tgt"])
        if not t:
            continue
        rank = _VIA_RANK.get(e.get("via") or "", 3)
        rel = e["rel"]
        if rel == "CONDITIONED_ON":
            txt = str(t.get("condition_text") or "")[:220]
        elif rel == "TRIGGERED_BY":
            txt = str(t.get("trigger_text") or t.get("description")
                      or t.get("name") or "")[:180]
        elif rel == "USES_TERM":
            txt = f"{t.get('term')} = " + str(t.get("definition") or "")[:200]
        elif rel == "COMPUTES":
            txt = str(t.get("name") or t.get("charge_type")
                      or t.get("rate_type") or "")[:140]
        else:  # MEASURES
            txt = str(t.get("name") or t.get("equipment_type") or "")[:120]
        if txt.strip():
            by_fact[e["src"]][rel].append((rank, txt))

    caps = {"CONDITIONED_ON": 3, "TRIGGERED_BY": 2, "USES_TERM": 4,
            "COMPUTES": 2, "MEASURES": 2}
    keys = {"CONDITIONED_ON": "linked_conditions", "TRIGGERED_BY": "linked_events",
            "USES_TERM": "linked_terms", "COMPUTES": "linked_computes",
            "MEASURES": "linked_measures"}
    out: dict[str, dict] = {}
    for fid, rels in by_fact.items():
        fields: dict[str, list[str]] = {}
        for rel, pairs in rels.items():
            pairs.sort(key=lambda p: p[0])
            seen: list[str] = []
            for _, txt in pairs:
                if txt not in seen:
                    seen.append(txt)
                if len(seen) >= caps[rel]:
                    break
            fields[keys[rel]] = seen
        out[fid] = fields
    return out


async def _enrich_evidence(store: AsyncCosmosStore, evs: list[dict],
                           scores: dict[str, float] | None = None,
                           *, with_links: bool = True) -> list[dict]:
    """Evidence items → projection rows compatible with ``_row_to_citation``
    (section context + parent fact + linked context) — the Python equivalent
    of the old shared Cypher projection."""
    if not evs:
        return []
    sections = await _fetch_by_ids(
        store, [e["section_id"] for e in evs if e.get("section_id")], kind="section")
    facts = await _fetch_by_ids(
        store, [e["fact_id"] for e in evs if e.get("fact_id")], kind="fact")
    links = (await _linked_context_fields(store, list(facts.keys()))
             if with_links else {})
    rows: list[dict] = []
    for e in evs:
        s = sections.get(e.get("section_id")) or {}
        n = facts.get(e.get("fact_id"))
        row = {
            "evidence_id": e["id"],
            "citation_kind": ("reference" if e.get("evidence_type") == "inferred_reference"
                              else "evidence_span"),
            "canonicalized": True,
            "confidence_tier": ("reference" if e.get("evidence_type") == "inferred_reference"
                                else "canonical"),
            "evidence_type": e.get("evidence_type"),
            "raw_label": None,
            "doc_id": e.get("doc_id"),
            "page_no": e.get("page_no"),
            "snippet": e.get("text_span"),
            "bbox_x0": e.get("bbox_x0"), "bbox_y0": e.get("bbox_y0"),
            "bbox_x1": e.get("bbox_x1"), "bbox_y1": e.get("bbox_y1"),
            "section_id": s.get("id"),
            "section_num": s.get("section_num"),
            "section_title": s.get("title"),
            "row_context": e.get("row_context"),
            "rects_json": e.get("rects_json"),
            "fact_labels": (n or {}).get("labels") or [],
            "fact_props": n,
            "score": (scores or {}).get(e["id"]),
        }
        row.update(links.get(e.get("fact_id") or "", {}))
        rows.append(row)
    return rows


def _block_row(b: dict, s: dict | None, score: float) -> dict:
    """Block item → projection row (evidence_id is the synthetic block id the
    /evidence endpoint resolves)."""
    return {
        "evidence_id": f"{b['doc_id']}:block:{b['id']}",
        "doc_id": b.get("doc_id"),
        "page_no": b.get("page_no"),
        "snippet": b.get("text"),
        "bbox_x0": b.get("bbox_x0"), "bbox_y0": b.get("bbox_y0"),
        "bbox_x1": b.get("bbox_x1"), "bbox_y1": b.get("bbox_y1"),
        "section_id": (s or {}).get("id"),
        "section_num": (s or {}).get("section_num"),
        "section_title": (s or {}).get("title"),
        "fact_labels": [],
        "fact_props": None,
        "score": score,
    }


async def _sections_of_blocks(store: AsyncCosmosStore,
                              blocks: list[dict]) -> dict[str, dict]:
    return await _fetch_by_ids(
        store, [b["section_id"] for b in blocks if b.get("section_id")],
        kind="section")


def _contains_any_clause(field: str, tokens: list[str], params: list[dict],
                         prefix: str) -> str:
    """Case-insensitive CONTAINS over any token — the candidate fetch that
    replaced the Lucene index (final ranking is Python keyword_score)."""
    ors = []
    for i, t in enumerate(tokens):
        pname = f"@{prefix}{i}"
        params.append({"name": pname, "value": t})
        ors.append(f"CONTAINS(c.{field}, {pname}, true)")
    return "(" + " OR ".join(ors) + ")" if ors else "false"


def _scope_sql(doc_ids: list[str] | None, params: list[dict]) -> str:
    if not doc_ids:
        return "true"
    params.append({"name": "@scope_ids", "value": list(doc_ids)})
    return "ARRAY_CONTAINS(@scope_ids, c.doc_id)"


# ---------------------------------------------------------------------------
# Embedding helper
# ---------------------------------------------------------------------------


async def _embed(client: AsyncOpenAI, model: str, text: str) -> list[float]:
    resp = await client.embeddings.create(model=model, input=[text])
    return resp.data[0].embedding
