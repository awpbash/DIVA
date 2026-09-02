"""Doctype-invariant structural writers + pure helpers, reused by the kb.*
loader (Cosmos port of the old Neo4j writers).

Everything here is doctype-agnostic: the item writers for the structural
skeleton (Document, Section, Block, Proposal, reference EvidenceSpans), the
store utilities (``_store`` / ``nuke``), the property-safety coercers
(``_props`` / ``_safe`` / ``_normalize_name``), the identity-key generators
(``_canonical_key`` / ``_canonical_site_key`` / ``_identity_text_key``), and
the pure text pair-finders the derive pass calls (``_cross_ref_pairs`` /
``_schedule_ref_pairs`` / ``_term_usage_pairs``). No fact-category logic and
no ontology coupling live here — the kb.load load-map interpreter owns that.

Port notes (Neo4j -> Cosmos document model, see pipeline/store/model.py):
  * MERGE + SET becomes read-merge-upsert where older passes' fields must
    survive (Document metadata, Proposal.status), plain upsert elsewhere.
  * The NEXT reading-order chain becomes ``block.order`` (an integer index);
    HAS_BLOCK becomes ``block.section_id``; HAS_SECTION order rides on the
    section item itself.
  * Embeddings live as an ``embedding`` field on section/block/evidence
    items. Neo4j's SET kept unlisted properties, so a re-load never wiped
    them; the port keeps that guarantee by re-attaching existing embeddings
    fetched in one per-doc query (``existing_embeddings``).
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..config import Config
from ..extraction.sections import Section, assign_section
from ..store import model
from ..store.client import CosmosStore, get_store


# ---------------------------------------------------------------------------
# Property coercion
# ---------------------------------------------------------------------------


def _safe(v: Any) -> Any:
    """Keep the Neo4j-era property shapes: primitives and lists of primitives
    pass through, nested structures are stored as JSON STRINGS. Cosmos could
    hold them natively, but every reader (rects_json, grid_json, payload_json)
    already parses strings — behavioural parity beats elegance here."""
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, list):
        if all(isinstance(x, (str, int, float, bool)) or x is None for x in v):
            return v
        return json.dumps(v, ensure_ascii=False)
    return json.dumps(v, ensure_ascii=False)


def _props(d: dict) -> dict:
    return {k: _safe(v) for k, v in d.items() if v is not None}


def _normalize_name(s: str | None) -> str:
    if not s:
        return ""
    return " ".join(s.lower().split())


_COSMOS_SYSTEM = ("_rid", "_self", "_etag", "_attachments", "_ts")


def strip_system(doc: dict | None) -> dict:
    """Drop Cosmos bookkeeping fields from a read item before re-upserting."""
    return {k: v for k, v in (doc or {}).items() if k not in _COSMOS_SYSTEM}


def existing_embeddings(store: CosmosStore, doc_id: str) -> dict[str, list]:
    """id -> embedding for every embedded item in one doc partition. Lets a
    re-load rewrite section/block/evidence items without losing the vectors
    (embed_doc re-fills missing ones from its disk cache anyway — this just
    avoids pointless churn)."""
    rows = store.fetch_embeddings(pk=doc_id)
    return {r["id"]: r["embedding"] for r in rows}


# ---------------------------------------------------------------------------
# Structural writers (one upsert batch per phase)
# ---------------------------------------------------------------------------


def _write_document(store: CosmosStore, *, doc_id: str, doctype: str,
                    analyzer_id: str, n_pages: int, n_facts: int,
                    title: str | None = None, source_path: str | None = None,
                    group: str | None = None,
                    extraction_mode: str = "legacy") -> None:
    # Human metadata (title/source_path/group) is set only when provided (from
    # the intake sidecar); otherwise the previously-stored value survives —
    # and so do fields later passes stamped (effective_date, conf_tokens,
    # declared_customer_id, ...), exactly like Neo4j's MERGE+SET.
    prior = strip_system(store.point(doc_id, doc_id))
    doc = model.item(model.DOCUMENT, doc_id, doc_id, {
        **prior,
        "doc_id": doc_id,
        "doctype": doctype,
        "analyzer_id": analyzer_id,
        "total_pages": n_pages,
        "n_facts": n_facts,
        "extraction_version": "v3",
        "extraction_mode": extraction_mode,
        "title": title or prior.get("title"),
        "source_path": source_path or prior.get("source_path"),
        "group": group or prior.get("group"),
    })
    aid = f"{doc_id}:agreement"
    prior_a = strip_system(store.point(aid, doc_id))
    agreement = model.item(model.AGREEMENT, aid, doc_id, {
        **prior_a,
        "agreement_id": aid,
        "doc_id": doc_id,
        "agreement_type": doctype,
        "title": title or prior_a.get("title") or doctype,
    })
    store.upsert_many([doc, agreement])


def _write_sections(store: CosmosStore, *, doc_id: str,
                    sections: list[Section],
                    keep_embeddings: dict[str, list] | None = None) -> None:
    if not sections:
        return
    keep = keep_embeddings or {}
    items = []
    for s in sections:
        props = {
            "section_id":   s.section_id,
            "doc_id":       s.doc_id,
            "agreement_id": f"{s.doc_id}:agreement",
            "section_num":  s.section_num,
            "title":        s.title,
            "section_kind": s.kind,
            "page_start":   s.page_start,
            "page_end":     s.page_end,
            "order":        s.order,
            "bbox_x0":      s.bbox_anchor[0],
            "bbox_y0":      s.bbox_anchor[1],
            "bbox_x1":      s.bbox_anchor[2],
            "bbox_y1":      s.bbox_anchor[3],
        }
        if s.section_id in keep:
            props["embedding"] = keep[s.section_id]
        items.append(model.item(model.SECTION, s.section_id, doc_id, _props(props)))
    store.upsert_many(items)


def _write_blocks(
    store: CosmosStore, *,
    doc_id: str,
    doc: dict,
    geometry: dict,
    sections: list[Section],
    keep_embeddings: dict[str, list] | None = None,
) -> int:
    """One block item per verbatim block in doc.json.

    ``order`` is the doc-wide reading-order index (replaces the NEXT chain);
    ``section_id`` replaces HAS_BLOCK. block_id stays ``<doc_id>:<local_id>``.
    Returns the number of block items written.
    """
    pages = doc.get("pages") or []
    if not pages:
        return 0

    geo_by_id = (geometry.get("blocks") or {}) if geometry else {}
    keep = keep_embeddings or {}

    flat: list[dict] = []
    for page in pages:
        page_no = int(page.get("page_no") or 0)
        for b in page.get("blocks") or []:
            local_id = b.get("id")
            if not local_id:
                continue
            geo = geo_by_id.get(local_id) or {}
            bbox = geo.get("bbox")
            section = assign_section(sections, page_no, bbox) if bbox else None
            grid = b.get("grid")
            flat.append({
                "block_id":      f"{doc_id}:{local_id}",
                "doc_id":        doc_id,
                "page_no":       page_no,
                "block_kind":    str(b.get("kind") or ""),
                "text":          str(b.get("text") or ""),
                "section_path":  list(b.get("section_path") or []),
                "grid_json":     json.dumps(grid, ensure_ascii=False) if grid else None,
                "section_id":    section.section_id if section else None,
                # bbox is needed so vector_search_blocks citations can draw
                # a highlight rectangle in the frontend PDF viewer.
                "bbox_x0":       float(bbox[0]) if bbox and len(bbox) >= 4 else None,
                "bbox_y0":       float(bbox[1]) if bbox and len(bbox) >= 4 else None,
                "bbox_x1":       float(bbox[2]) if bbox and len(bbox) >= 4 else None,
                "bbox_y1":       float(bbox[3]) if bbox and len(bbox) >= 4 else None,
            })

    if not flat:
        return 0

    items = []
    for order, r in enumerate(flat):
        props = {**r, "order": order}
        if r["block_id"] in keep:
            props["embedding"] = keep[r["block_id"]]
        items.append(model.item(model.BLOCK, r["block_id"], doc_id, _props(props)))
    store.upsert_many(items)
    return len(flat)


# ---------------------------------------------------------------------------
# Quarantine proposals
# ---------------------------------------------------------------------------


def _write_proposals(store: CosmosStore, *, doc_id: str,
                     quarantined: list[dict]) -> int:
    """Quarantined facts land as proposal items under the document —
    queryable for review, excluded from retrieval. ``status`` survives
    re-loads (a human 'rejected' mark isn't reset by the next ingest)."""
    if not quarantined:
        return 0
    items = []
    for i, q in enumerate(quarantined, start=1):
        f = q["fact"]
        pid = f"{doc_id}:proposal:{f.get('id') or ('q%03d' % i)}"
        prior = strip_system(store.point(pid, doc_id))
        items.append(model.item(model.PROPOSAL, pid, doc_id, {
            "proposal_id": pid,
            "doc_id": doc_id,
            "proposal_kind": "fact",
            "category": str(f.get("category")),
            "reason": q["reason"],
            "payload_json": json.dumps(f, ensure_ascii=False),
            "status": prior.get("status") or "pending",
        }))
    store.upsert_many(items)
    return len(items)


# Pull "Section 9.3" / "Clause 6.1(a)" -> "9.3" / "6.1(a)" so we can match
# against the deterministic Section.section_num key.
_REF_SECTION_NUM_RE = re.compile(r"(\d+(?:\.\d+)*(?:\([a-zA-Z]+\))?)")


def _extract_section_num(ref_text: str) -> str | None:
    m = _REF_SECTION_NUM_RE.search(ref_text or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Mention geometry — block_ids -> highlight rects + envelope
# ---------------------------------------------------------------------------


def _mention_rects(
    block_ids: list[str], geometry: dict,
) -> tuple[list[dict], list[float] | None]:
    """Derive ``(rects, envelope_bbox)`` for a mention from its block_ids via
    the geometry sidecar — the same deterministic block lookup the evidence
    path uses. ``rects`` is one ``{page_no, bbox}`` per resolvable block;
    ``envelope`` is the union. Returns ``([], None)`` when nothing resolves
    (the mention still loads, just without a highlight rect)."""
    geo_by_id = (geometry.get("blocks") or {}) if geometry else {}
    rects: list[dict] = []
    for bid in block_ids or []:
        geo = geo_by_id.get(bid) or {}
        bbox = geo.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        rects.append({"page_no": int(geo.get("page_no") or 0),
                      "bbox": [float(x) for x in bbox[:4]]})
    if not rects:
        return [], None
    env = [min(r["bbox"][0] for r in rects), min(r["bbox"][1] for r in rects),
           max(r["bbox"][2] for r in rects), max(r["bbox"][3] for r in rects)]
    return rects, env


def _write_reference_edges(
    store: CosmosStore, *, doc_id: str, facts: list[dict],
    sections: list[Section], geometry: dict,
) -> int:
    """Promote ``reference`` facts to reference evidence items.

    A reference fact (e.g. ``ref_text='Section 9.3'``) does NOT become a
    fact item. For each source where the reference appears we upsert an
    evidence item (the *origin* — where the reference is written) whose
    ``target_section_id`` + ``reference_text`` fields ARE the old
    REFERENCES_SECTION edge.

    Skips references whose target section_num doesn't match any Section in
    the doc; those never block the load.
    """
    refs = [f for f in facts if f.get("category") == "reference"]
    if not refs:
        return 0

    section_by_num: dict[str, str] = {s.section_num: s.section_id for s in sections}
    agreement_id = f"{doc_id}:agreement"
    items: list[dict] = []

    for fact in refs:
        n = fact.get("normalised") or {}
        if n.get("target_kind") not in ("section", "clause"):
            continue
        ref_text = str(n.get("ref_text") or fact.get("value") or "")
        section_num = _extract_section_num(ref_text)
        if not section_num:
            continue
        target_section_id = section_by_num.get(section_num)
        if not target_section_id:
            continue

        for i, src in enumerate(fact.get("sources") or []):
            ev_id = f"{doc_id}:reference:{fact['id']}:e{i + 1}"
            block_ids = list(src.get("block_ids") or [])
            rects, env = _mention_rects(block_ids, geometry)
            src_bbox = src.get("bbox") or []
            bbox = src_bbox if len(src_bbox) >= 4 else (env or [])
            page_no = int(src.get("page_no") or (rects[0]["page_no"] if rects else 0))
            origin = assign_section(sections, page_no, bbox)
            items.append(model.item(model.EVIDENCE, ev_id, doc_id, _props({
                "evidence_id":       ev_id,
                "doc_id":            doc_id,
                "agreement_id":      agreement_id,
                "raw_fact_id":       src.get("raw_fact_id"),
                "page_no":           page_no,
                "section_id":        origin.section_id if origin else None,
                "target_section_id": target_section_id,
                "reference_text":    ref_text,
                "text_span":         str(src.get("snippet") or ""),
                "snippet_ok":        True,
                "block_ids":         block_ids,
                "bbox_x0":           float(bbox[0]) if len(bbox) >= 4 else None,
                "bbox_y0":           float(bbox[1]) if len(bbox) >= 4 else None,
                "bbox_x1":           float(bbox[2]) if len(bbox) >= 4 else None,
                "bbox_y1":           float(bbox[3]) if len(bbox) >= 4 else None,
                "rects_json":        json.dumps(rects) if rects else None,
                "evidence_type":     "inferred_reference",
            })))

    if items:
        store.upsert_many(items)
    return len(items)


# ---------------------------------------------------------------------------
# Store utilities
# ---------------------------------------------------------------------------


def _store(cfg: Config) -> CosmosStore:
    return get_store(cfg)


def nuke(cfg: Config) -> int:
    """Drop + recreate the container. Returns the prior item count."""
    return get_store(cfg).nuke()


# ---------------------------------------------------------------------------
# Text-match pair-finders: USES_TERM, REFERENCES_SCHEDULE, cross-ref
# ---------------------------------------------------------------------------


# Defined-term usage matching is case-SENSITIVE on purpose: capitalised
# usage is the contract-drafting convention for "as defined above" ("the
# Consumption Charge" vs "a consumption charge"). Word-bounded so "Deposit"
# never matches inside "Depositary".
_TERM_MIN_LEN = 3
# A term matched by more than this fraction of an agreement's facts is a
# hub ("Agreement" matches half the document) — non-discriminative, so no
# edges are created for it. Dropped terms are reported in the counters.
_TERM_HUB_FRACTION = 0.10

_MATCH_QUOTE_TRANS = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
})


def _norm_match_text(s: str) -> str:
    """Normalise for term matching: curly->straight quotes, collapse ws."""
    return re.sub(r"\s+", " ", s.translate(_MATCH_QUOTE_TRANS)).strip()


def _term_usage_pairs(
    fact_texts: list[tuple[str, str]],
    terms: list[tuple[str, str]],
    *,
    hub_fraction: float = _TERM_HUB_FRACTION,
) -> tuple[list[tuple[str, str]], list[str]]:
    """(fact_key, term_key) pairs where the term name occurs in the fact's
    evidence text. Returns (pairs, hub_terms_dropped). Pure — unit-testable
    without a store. Self-references (fact IS the term node) are skipped.
    """
    matches: dict[str, list[tuple[str, str]]] = {}
    n_facts = len(fact_texts)
    normed = [(fid, _norm_match_text(text)) for fid, text in fact_texts]
    for tid, term in terms:
        term_n = _norm_match_text(term)
        if len(term_n) < _TERM_MIN_LEN:
            continue
        pat = re.compile(rf"(?<!\w){re.escape(term_n)}(?!\w)")
        hits = [
            (fid, tid) for fid, text in normed
            if fid != tid and pat.search(text)
        ]
        if hits:
            matches.setdefault(term_n, []).extend(hits)
    pairs: list[tuple[str, str]] = []
    hubs: list[str] = []
    cap = max(1.0, hub_fraction * n_facts)
    for term_n, hits in matches.items():
        if len(hits) > cap:
            hubs.append(term_n)
        else:
            pairs.extend(hits)
    return pairs, hubs


_SCHEDULE_PREFIX_RE = re.compile(r"(?i)^(schedule|figure|appendix|annex|exhibit)\b")


def _schedule_ref_pairs(
    fact_texts: list[tuple[str, str]],
    schedules: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """(fact_key, schedule_key) pairs where the fact's evidence references
    the schedule's label. ``schedule_num`` as harvested usually carries its
    own prefix ('Schedule 1B', 'Figure 1A'); a bare '1B' gets 'Schedule '
    prepended. Word-bounded on the trailing edge so 'Schedule 1' does NOT
    match inside 'Schedule 1B'."""
    pairs: list[tuple[str, str]] = []
    normed = [(fid, _norm_match_text(text)) for fid, text in fact_texts]
    for sid, num in schedules:
        label = _norm_match_text(str(num))
        if not _SCHEDULE_PREFIX_RE.match(label):
            label = f"Schedule {label}"
        pat = re.compile(rf"(?<!\w){re.escape(label)}(?!\w)")
        pairs.extend(
            (fid, sid) for fid, text in normed
            if fid != sid and pat.search(text)
        )
    return pairs


# Cross-reference fact-to-fact linking. Contracts connect themselves with
# explicit pointers ("subject to Section 9.3", "notwithstanding Clause 8.2").
# The loader already resolves these to reference evidence items but never hops
# the cited section to its FACTS — so a far-clause condition/event/formula stays
# disconnected from the obligation that points at it. This closes that gap
# deterministically (the document's own wording, no LLM). via='cross_ref' marks
# it a recall tier — looser than same_block, like same_section — so consumers
# can weight it.
_SECTION_REF_RE = re.compile(r"(?i)\b(?:section|clause)\s+(\d+(?:\.\d+)*)\b")
_LEADING_NUM_RE = re.compile(r"\d+(?:\.\d+)*")


def _leading_section_num(section_num: str) -> str | None:
    """Bare dotted number at the head of a section_num ('9.3 Payment' -> '9.3'),
    so a 'Section 9.3' reference resolves even when the stored num carries a
    trailing title."""
    m = _LEADING_NUM_RE.match((section_num or "").strip())
    return m.group(0) if m else None


def _cross_ref_pairs(
    fact_texts: list[tuple[str, str]],
    sections: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """(fact_key, target_section_id) pairs where a fact's evidence text
    explicitly references that section by number ('Section 9.3', 'Clause 8').
    Pure — mirrors _schedule_ref_pairs.

    The 'section'/'clause' prefix is required, so a bare '9.3' inside a value
    never matches — that's the precision guard against spurious links.
    """
    by_num: dict[str, str] = {}
    for sid, num in sections:
        key = _leading_section_num(str(num))
        if key:
            by_num.setdefault(key, sid)
    pairs: list[tuple[str, str]] = []
    for fid, text in fact_texts:
        if not text:
            continue
        seen: set[str] = set()
        for m in _SECTION_REF_RE.finditer(text):
            sid = by_num.get(m.group(1))
            if sid and sid not in seen:
                seen.add(sid)
                pairs.append((fid, sid))
    return pairs


# ---------------------------------------------------------------------------
# Identity-key generators (suffix-invariant party, address-like site)
# ---------------------------------------------------------------------------


def _canonical_key(name: str | None, suffixes: list[str]) -> str:
    """Suffix-invariant canonical key. Lowercase, strip dots/commas,
    collapse whitespace, then peel legal suffixes (longest first, applied
    repeatedly — handles 'X Co Pte Ltd')."""
    norm = " ".join(
        (name or "").replace(",", " ").replace(".", " ").lower().split()
    )
    ordered = sorted((s.lower() for s in suffixes), key=len, reverse=True)
    changed = True
    while changed:
        changed = False
        for suf in ordered:
            if norm.endswith(" " + suf) and len(norm) > len(suf) + 2:
                norm = norm[: -(len(suf) + 1)].rstrip()
                changed = True
    return norm


def _identity_text_key(value: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split())


def _canonical_site_key(address: str | None, name: str | None = None) -> str:
    key = _identity_text_key(address or name)
    if len(key) < 8 or not any(ch.isdigit() for ch in key):
        return ""
    return key
