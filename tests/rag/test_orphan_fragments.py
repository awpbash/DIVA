"""Unit tests for the orphan-fragment recall tool's query tokenizer.

`find_orphan_fragments` keyword-matches orphan FactMention spans. The match
set is `_query_words(query)` — 4+ char tokens, lowercased, deduped. The graph
match itself is exercised by the live-rebuild verification.
"""
from __future__ import annotations

from api.rag.tools import (
    _orphan_is_substantive,
    _query_words,
    _row_to_citation,
)


def test_drops_short_tokens():
    # "Ton" (3) drops; "Hours"/"Refrigerating" stay.
    assert _query_words("Refrigerating Ton-Hours") == ["hours", "refrigerating"]


def test_lowercases_and_dedupes():
    assert _query_words("Water water WATER temperature") == ["temperature", "water"]


def test_all_short_returns_empty():
    assert _query_words("a be the of in") == []


def test_numbers_are_tokens():
    assert _query_words("clause 1234 value") == ["1234", "clause", "value"]


def test_empty_and_none():
    assert _query_words("") == []
    assert _query_words(None) == []


def test_orphan_substantive_admits_provisions_dropped_as_other():
    # The substantive provisions the LLM dumped in the 'other' bucket — these
    # must now be reachable (they were blanket-excluded by label before).
    assert _orphan_is_substantive("COOLING AND CHILLED WATER SUPPLY AGREEMENT")
    assert _orphan_is_substantive(
        "This Agreement will be governed by and construed in accordance with the "
        "laws of Singapore.")
    assert _orphan_is_substantive("Heat and mass balance")          # short deliverable, no digit


def test_orphan_substantive_admits_dropped_measurements():
    # The original use case: a dropped value/number stays reachable via the digit.
    assert _orphan_is_substantive("200 RT")
    assert _orphan_is_substantive("1600A")


def test_orphan_substantive_rejects_form_furniture():
    # Form labels that share the 'other' bucket but carry no real content.
    assert not _orphan_is_substantive("Signed by")
    assert not _orphan_is_substantive("")
    assert not _orphan_is_substantive("   ")


def test_row_to_citation_marks_fact_mentions_as_raw_atoms():
    c = _row_to_citation({
        "evidence_id": "doc:m:h095",
        "doc_id": "doc",
        "page_no": 12,
        "snippet": "based on Refrigerating Ton-Hours utilised",
        "raw_label": "measurement",
        "fact_labels": [],
        "fact_props": None,
        "score": 0.55,
    })
    assert c.citation_kind == "fact_mention"
    assert c.canonicalized is False
    assert c.confidence_tier == "raw"
    assert c.raw_label == "measurement"


def test_row_to_citation_marks_layout_atoms_from_synthetic_ids():
    block = _row_to_citation({
        "evidence_id": "doc:block:doc:b001",
        "doc_id": "doc",
        "page_no": 1,
        "snippet": "diagram text",
        "fact_labels": [],
        "fact_props": None,
        "score": 0.6,
    })
    heading = _row_to_citation({
        "evidence_id": "doc:6.2:heading",
        "doc_id": "doc",
        "page_no": 1,
        "snippet": "6.2 Charges",
        "fact_labels": [],
        "fact_props": None,
        "score": 0.6,
    })
    assert block.citation_kind == "block"
    assert block.confidence_tier == "layout"
    assert heading.citation_kind == "section_heading"
    assert heading.confidence_tier == "layout"
