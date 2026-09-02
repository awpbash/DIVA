"""Unit tests for the citation gate — deterministic anti-fabrication.

The synth LLM is told to cite `[ev:doc:category:fact_id:span_id]` copied from
the evidence block. For aggregate counts it has no real id and tends to invent
`…:unknown:unknown`. `forward_tokens(..., allowed_ids)` strips any id not in the
retrieved bundle so a citation that resolves to nothing never reaches the user.
"""
from __future__ import annotations

import asyncio

from api.rag.citations import (
    _strip_unknown_tags,
    extract_cited_ids,
    forward_tokens,
)

ALLOWED = {"b9:obligation:f1:e1", "b9:rate:f2:e1"}


def test_keeps_known_strips_unknown():
    text = ("Real [ev:b9:obligation:f1:e1] and fabricated "
            "[ev:27090:obligation:unknown:unknown] count.")
    out = _strip_unknown_tags(text, ALLOWED)
    assert "[ev:b9:obligation:f1:e1]" in out
    assert "unknown:unknown" not in out
    # the known tag is the only [ev: left
    assert out.replace("[ev:b9:obligation:f1:e1]", "").count("[ev:") == 0


def test_none_allowed_is_noop():
    text = "x [ev:anything:here:f:e1] y"
    assert _strip_unknown_tags(text, None) == text


def test_extract_cited_ids_unchanged():
    assert extract_cited_ids("[ev:a:b:c:d][ev:e:f:g:h]") == ["a:b:c:d", "e:f:g:h"]


def _collect(chunks: list[str], allowed, swap=None, rescue=None) -> str:
    async def up():
        for c in chunks:
            yield c

    async def run():
        return "".join([p async for p in forward_tokens(
            up(), allowed, swap=swap, rescue=rescue)])

    return asyncio.run(run())


def test_forward_tokens_strips_unknown_split_across_chunks():
    # The fabricated tag is split across token boundaries; it must still be
    # reassembled and stripped, while the real tag survives.
    chunks = ["The total is 90 [ev:27090:obligation:unk",
              "nown:unknown] per [ev:b9:rate:f2:e1] doc."]
    out = _collect(chunks, ALLOWED)
    assert "unknown:unknown" not in out
    assert "[ev:b9:rate:f2:e1]" in out
    assert "The total is 90" in out


def test_forward_tokens_passthrough_without_allowed():
    out = _collect(["hi [ev:x:y:z:w] there"], None)
    assert out == "hi [ev:x:y:z:w] there"


# ---------------------------------------------------------------------------
# Verified-record preference: a raw-clause citation upgrades to the verified
# knowledge-base record when the answer text states the verified value.
# ---------------------------------------------------------------------------

RAW = "b9:party:f9:e1"
OPS = "ops:b9:customer_name"
SWAP_ALLOWED = {RAW, OPS, "b9:rate:f2:e1"}
SWAP = {RAW: (OPS, "northwind logistics")}


def test_swap_upgrades_raw_citation_when_value_in_claim():
    out = _collect(["The customer is NORTHWIND LOGISTICS ", f"[ev:{RAW}]."],
                   SWAP_ALLOWED, SWAP)
    assert f"[ev:{OPS}]" in out
    assert f"[ev:{RAW}]" not in out


def test_swap_skipped_when_claim_states_something_else():
    # Same clause cited for the ADDRESS: the name consensus must not attach.
    out = _collect([f"The registered office is at 45 Alexandra Terrace [ev:{RAW}]."],
                   SWAP_ALLOWED, SWAP)
    assert f"[ev:{RAW}]" in out
    assert f"[ev:{OPS}]" not in out


def test_swap_dedupes_adjacent_tags_resolving_to_same_record():
    swap = {RAW: (OPS, "northwind logistics"),
            "b9:rate:f2:e1": (OPS, "northwind logistics")}
    out = _collect([f"Customer: NORTHWIND LOGISTICS [ev:{RAW}][ev:b9:rate:f2:e1]."],
                   SWAP_ALLOWED, swap)
    assert out.count(f"[ev:{OPS}]") == 1
    assert f"[ev:{RAW}]" not in out and "[ev:b9:rate:f2:e1]" not in out


def test_swap_works_across_chunk_boundaries():
    out = _collect(["The customer is NORTH", "WIND LOGISTICS [ev:",
                    f"{RAW}] per the agreement."], SWAP_ALLOWED, SWAP)
    assert f"[ev:{OPS}]" in out


def test_same_id_recited_after_text_is_kept():
    # prev-tag dedupe only collapses ADJACENT duplicates, not legit re-cites.
    out = _collect([f"NORTHWIND LOGISTICS [ev:{RAW}] pays monthly. "
                    f"NORTHWIND LOGISTICS [ev:{RAW}] also owns the unit."],
                   SWAP_ALLOWED, SWAP)
    assert out.count(f"[ev:{OPS}]") == 2


def test_verified_swap_map_same_doc_and_value_only():
    from api.rag.schemas import Citation
    from api.rag.synth import _verified_swap_map

    def cit(eid, doc, snippet="", trust=None, value=None):
        return Citation(evidence_id=eid, doc_id=doc, page_no=1, snippet=snippet,
                        field_trust=trust, field_value=value)

    verified = cit(OPS, "b9", "NORTHWIND LOGISTICS as Customer",
                   trust="human_validated", value="NORTHWIND LOGISTICS")
    short = cit("ops:b9:temp", "b9", "7", trust="human_validated", value="7")
    raw_match = cit(RAW, "b9", "between Alder Grove and NORTHWIND LOGISTICS")
    raw_other_doc = cit("zz:party:f1:e1", "zz", "NORTHWIND LOGISTICS mentioned")
    raw_no_value = cit("b9:date:f3:e1", "b9", "dated 3 April 2020")
    swap = _verified_swap_map([verified, short, raw_match, raw_other_doc, raw_no_value])
    assert swap == {RAW: (OPS, "northwind logistics")}   # short "7" never matches


# ---------------------------------------------------------------------------
# Value-rescue: a dropped (mis-copied / invented) id becomes a real citation
# for the value the claim states, so a provable fact never loses its chip.
# ---------------------------------------------------------------------------

# rank 0 = verified customer record, rank 1 = raw supplier party clause.
RESCUE = [("northwind logistics", OPS, 0),
          ("alder grove utilities pte ltd", "b9:organization:f001:e1", 1)]
RESCUE_ALLOWED = {OPS, "b9:organization:f001:e1"}


def test_rescue_dropped_id_to_verified_record():
    # Model invents an ops id that was never retrieved; it would be stripped.
    out = _collect(["The customer is NORTHWIND LOGISTICS [ev:ops:b9:made_up_field]."],
                   RESCUE_ALLOWED, rescue=RESCUE)
    assert f"[ev:{OPS}]" in out                       # rescued to the real record
    assert "made_up_field" not in out


def test_rescue_picks_value_nearest_the_tag():
    # Both values sit in the window; the supplier tag must grab the supplier,
    # not the earlier-mentioned customer.
    out = _collect(["NORTHWIND LOGISTICS is the customer and Alder Grove Utilities Pte Ltd "
                    "is the supplier [ev:ops:b9:supplier_name]."],
                   RESCUE_ALLOWED, rescue=RESCUE)
    assert "[ev:b9:organization:f001:e1]" in out
    assert f"[ev:{OPS}]" not in out


def test_rescue_skipped_when_value_stated_too_far_back():
    # The value is in the 300-char window but far from the tag — not this claim.
    filler = " and various other clauses apply here for a while" * 4
    out = _collect([f"NORTHWIND LOGISTICS is named.{filler} [ev:ops:b9:bogus]."],
                   RESCUE_ALLOWED, rescue=RESCUE)
    assert f"[ev:{OPS}]" not in out
    assert "bogus" not in out


def test_rescue_no_match_drops_as_before():
    out = _collect(["The total count is 3 [ev:agg:unknown:unknown:unk]."],
                   RESCUE_ALLOWED, rescue=RESCUE)
    assert "[ev:" not in out                          # nothing to rescue → dropped


def test_rescue_leaves_valid_ids_untouched():
    out = _collect([f"Customer is NORTHWIND LOGISTICS [ev:{OPS}]."],
                   RESCUE_ALLOWED, rescue=RESCUE)
    assert out.count(f"[ev:{OPS}]") == 1


def test_rescue_index_sources_verified_and_party_names():
    from api.rag.schemas import Citation
    from api.rag.synth import _rescue_index

    def cit(eid, summary="", trust=None, value=None):
        return Citation(evidence_id=eid, doc_id="b9", page_no=1, snippet="",
                        fact_summary=summary, field_trust=trust, field_value=value)

    verified = cit(OPS, "[HUMAN-VERIFIED ✓ 67%] Customer = NORTHWIND LOGISTICS",
                   trust="human_validated", value="NORTHWIND LOGISTICS")
    supplier = cit("b9:organization:f001:e1",
                   "Alder Grove Utilities Pte Ltd · supplier · 88 Harbourfront Avenue")
    ai_junk = cit("ops:b9:x", "[AI-extracted, unreviewed] Customer = COMPANY",
                  value="COMPANY")                     # excluded: not verified, no party summary
    idx = _rescue_index([verified, supplier, ai_junk])
    assert ("northwind logistics", OPS, 0) in idx
    assert ("alder grove utilities pte ltd", "b9:organization:f001:e1", 1) in idx
    assert all("company" != v for v, _, _ in idx)     # placeholder junk kept out
    assert idx[0][2] == 0                              # verified sorts first
