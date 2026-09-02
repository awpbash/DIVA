"""The citation junk-filter in field_llm._resolve_evidence.

The model cites block ids per value; every cited id becomes a highlight rect.
When it pads a correct citation with unrelated blocks, the unrelated rects
must be dropped — but ONLY when a strong block proves which citation is real.
With no strong block, all citations are kept for the human reviewer.
"""
from pipeline.kb.field_llm import _recall, _resolve_evidence

import json


def _bm():
    return {
        "p46b2": {"page_no": 46, "bbox": [0.1, 0.2, 0.9, 0.4],
                  "body": "(table)\n| Chillers |  | 3 |  |  | Ch 1 and Ch 2 to be "
                          "replaced with new chillers from Jun 2016 |",
                  "text": "soup text the OCR saw"},
        "p33b6": {"page_no": 33, "bbox": [0.1, 0.5, 0.5, 0.55],
                  "body": "18.7 Regulatory Bodies", "text": "18.7 Regulatory Bodies"},
        "p35b8": {"page_no": 35, "bbox": [0.1, 0.6, 0.5, 0.65],
                  "body": "18.14 Entire Agreement", "text": "18.14 Entire Agreement"},
        "p3b2": {"page_no": 3, "bbox": [0.2, 0.1, 0.8, 0.15],
                 "body": "Alder Grove Utilities Pte Ltd (as Supplier)",
                 "text": "Alder Grove Utilities Pte Ltd (as Supplier)"},
    }


def _pages(v, bid):
    return (json.loads(v["rects"]) if v["rects"] else [])


def test_junk_siblings_dropped_when_strong_block_exists():
    v = {"value": "Chillers",
         "snippet": "| Chillers |  | 3 |  |  | Ch 1 and Ch 2 to be replaced with new chillers from Jun 2016 |",
         "page": 33, "blocks": ["p33b6", "p35b8", "p46b2"]}
    ev, ok = _resolve_evidence(v, _bm(), {})
    rects = json.loads(ev["rects"])
    assert ok
    assert [r["page_no"] for r in rects] == [46]
    assert ev["page"] == 46          # page label follows the kept rect


def test_formatting_drift_still_counts_as_strong():
    # "as Supplier" vs "(as Supplier)" — token recall shrugs at punctuation.
    v = {"value": "Alder Grove Utilities Pte Ltd",
         "snippet": "Alder Grove Utilities Pte Ltd as Supplier",
         "page": 3, "blocks": ["p3b2", "p33b6"]}
    ev, _ = _resolve_evidence(v, _bm(), {})
    assert [r["page_no"] for r in json.loads(ev["rects"])] == [3]


def test_no_strong_block_keeps_all_citations():
    # Paraphrased snippet matching nothing well: never guess, keep everything
    # for the human.
    v = {"value": "some obligation", "snippet": "a heavily paraphrased summary",
         "page": 33, "blocks": ["p33b6", "p35b8"]}
    ev, _ = _resolve_evidence(v, _bm(), {})
    assert sorted(r["page_no"] for r in json.loads(ev["rects"])) == [33, 35]


def test_value_match_alone_protects_a_block():
    # A block that contains the VALUE (but not the quote) is not junk.
    v = {"value": "Regulatory Bodies", "snippet": "| Chillers |  | 3 |  |  | Ch 1 and Ch 2 to be replaced with new chillers from Jun 2016 |",
         "page": 33, "blocks": ["p33b6", "p46b2"]}
    ev, _ = _resolve_evidence(v, _bm(), {})
    assert sorted(r["page_no"] for r in json.loads(ev["rects"])) == [33, 46]


def test_recall_bounds():
    assert _recall("", "anything") == 1.0
    assert _recall("chillers replaced 2016", "18.7 Regulatory Bodies") == 0.0
    assert _recall("Alder Grove Utilities", "Alder Grove Utilities Pte Ltd (as Supplier)") == 1.0


# --------------------------------------------------------------------------- #
# Stale grid overlays. table_repair grids are keyed by the reader's global
# block numbering; a re-read (RapidOCR -> CU) renumbers every block, so an old
# grid can land on an unrelated block and paste a table over it — the model
# then cites markers whose rects point at that unrelated text.
# --------------------------------------------------------------------------- #
from pipeline.kb.field_llm import _render


_GRID = {"n_rows": 2, "n_columns": 3,
         "headers": ["Equipment", "Brand", "Quantity"],
         "rows": [["Chiller - Duty", "Daikin", "2"],
                  ["Chiller - Standby", "Trane", "1"]]}


def _one_page(text):
    return [{"page_no": 1, "blocks": [{"text": text, "bbox": [0.1, 0.1, 0.9, 0.2]}]}]


def test_stale_grid_overlay_guarded():
    text, block_map, _ = _render(_one_page("18.7 Regulatory Bodies"), {"b0001": _GRID})
    assert "(table)" not in text
    assert block_map["p1b0"]["body"] == "18.7 Regulatory Bodies"


def test_matching_grid_still_overlays():
    soup = "Equipment Brand Quantity Chiller Duty Daikin 2 Chiller Standby Trane 1"
    text, block_map, _ = _render(_one_page(soup), {"b0001": _GRID})
    assert "(table)" in text
    assert block_map["p1b0"]["body"].startswith("(table)")
