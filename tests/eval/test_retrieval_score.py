"""Unit tests for the retrieval-only scorer.

The contract under test: recall is judged against the WHOLE citation bundle
(did retrieval *fetch* the answer span?), independent of any synth answer.
Pure function, no network — runs in the free test tier.
"""
from __future__ import annotations

import pytest

score_retrieval = pytest.importorskip(
    "eval.run", reason="the live chat harness is not in this checkout"
).score_retrieval

DOC_MAP = {"dummy1": "DOC1", "dummy2": "DOC2"}


def _case(**over):
    base = {
        "id": "qX",
        "expected_doc": "dummy1",
        "expected_evidence": {"page": 5, "must_contain": ["chiller"]},
    }
    base.update(over)
    return base


def _bundle_result(**over):
    cit = {
        "evidence_id": "e1", "doc_id": "DOC1", "page_no": 5,
        "snippet": "400 RT water-cooled chiller system", "fact_summary": "",
    }
    cit.update(over.pop("citation", {}))
    res = {"citations": [cit], "tool_calls": [{"tool": "lookup_facts_by_label"}]}
    res.update(over)
    return res


def test_pass_when_span_in_bundle():
    s = score_retrieval(_case(), _bundle_result(), DOC_MAP)
    assert s["pass"] is True
    assert s["recall_doc"] and s["recall_snippet"] and s["recall_page"]


def test_fail_when_snippet_not_retrieved():
    res = _bundle_result(citation={"snippet": "indemnity and liability clause"})
    s = score_retrieval(_case(), res, DOC_MAP)
    assert s["pass"] is False
    assert s["recall_snippet"] is False
    assert "snippet not retrieved" in s["issues"]


def test_fail_when_wrong_doc():
    res = _bundle_result(citation={"doc_id": "DOC2"})
    s = score_retrieval(_case(), res, DOC_MAP)
    assert s["pass"] is False
    assert s["recall_doc"] is False


def test_empty_bundle_fails():
    s = score_retrieval(_case(), {"citations": [], "tool_calls": []}, DOC_MAP)
    assert s["pass"] is False
    assert s["recall_any"] is False


def test_tool_selection_checked_when_declared():
    res = _bundle_result(tool_calls=[{"tool": "vector_search_sections"}])
    s = score_retrieval(_case(expected_tools=["lookup_facts_by_label"]), res, DOC_MAP)
    assert s["tool_ok"] is False
    assert s["pass"] is False


def test_evidence_id_recall_when_silver_labels_present():
    # Gold ids e1 + e2, bundle only has e1 → partial recall, fails.
    s = score_retrieval(
        _case(expected_evidence_ids=["e1", "e2"]), _bundle_result(), DOC_MAP,
    )
    assert s["recall_evidence_ids"] is False
    assert s["eid_recall_frac"] == 0.5
    assert "gold ids missed" in s["issues"]


def test_evidence_id_recall_full():
    res = _bundle_result(citations=[
        {"evidence_id": "e1", "doc_id": "DOC1", "page_no": 5, "snippet": "chiller"},
        {"evidence_id": "e2", "doc_id": "DOC1", "page_no": 6, "snippet": "pump"},
    ], tool_calls=[{"tool": "lookup_facts_by_label"}])
    s = score_retrieval(_case(expected_evidence_ids=["e1", "e2"]), res, DOC_MAP)
    assert s["recall_evidence_ids"] is True
    assert s["eid_recall_frac"] == 1.0
