"""Unit tests for the currency-tag rendering on citations.

The display defect under test: a value re-stated verbatim by a later document
("S$0.0241/RTh" in every supplemental) must NEVER be shown as `superseded` —
users read "current 0.0241 superseded the earlier 0.0241" as a data bug. The
timeline now materialises a third status, `restated`, and the tools render it
as a CURRENT variant. Pure functions, no Neo4j — the free test tier.
"""
from __future__ import annotations

from api.rag.tools import _CURRENCY_TAGS, _currency_prefixed


def test_active_renders_current():
    out = _currency_prefixed(
        {"status": "active", "effective_date": "2022-10-01"}, "Rate 0.0241")
    assert out == "[CURRENT · effective 2022-10-01] Rate 0.0241"


def test_restated_renders_as_current_variant_never_superseded():
    out = _currency_prefixed(
        {"status": "restated", "effective_date": "2020-12-18"}, "Rate 0.0241")
    assert out == ("[CURRENT (re-stated later; value unchanged) · "
                   "effective 2020-12-18] Rate 0.0241")
    assert "superseded" not in out


def test_superseded_still_renders():
    out = _currency_prefixed(
        {"status": "superseded", "effective_date": "2019-07-26"}, "Rate 0.0205")
    assert out == "[superseded · effective 2019-07-26] Rate 0.0205"


def test_unknown_or_missing_status_is_untagged():
    assert _currency_prefixed({"status": "pending", "effective_date": "2020-01-01"},
                              "x") == "x"
    assert _currency_prefixed({"status": "active"}, "x") == "x"   # no effective date
    assert _currency_prefixed(None, "x") == "x"


def test_tag_map_covers_the_three_statuses():
    assert set(_CURRENCY_TAGS) == {"active", "restated", "superseded"}
