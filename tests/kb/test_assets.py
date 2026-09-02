"""Drawing/schematic page detection over the cached OCR layer (deterministic)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from pipeline.kb import assets


def _page(page_no=18, blocks=None):
    return {"page_no": page_no, "blocks": blocks or []}


def _fig(x0, y0, x1, y1):
    return {"kind": "figure", "bbox": [x0, y0, x1, y1]}


def test_full_page_schematic_detected_with_heading_title():
    page = _page(blocks=[
        {"kind": "heading", "text": "ATTACHMENT 4: COOLING WATER SECTION"},
        {"kind": "header", "text": "rev. 3"},
        _fig(0.05, 0.1, 0.95, 0.95),                 # ~0.77 of the page
    ])
    a = assets.page_asset(page)
    assert a is not None
    assert a["title"] == "ATTACHMENT 4: COOLING WATER SECTION"
    assert a["kind"] == "diagram" and a["page_no"] == 18


def test_caption_titles_a_headingless_figure_page():
    page = _page(blocks=[
        {"kind": "caption", "text": "Standard Detail for Pipe Penetration"},
        _fig(0.1, 0.2, 0.9, 0.9),
    ])
    assert assets.page_asset(page)["title"] == "Standard Detail for Pipe Penetration"


def test_untitled_figure_page_gets_a_page_fallback_title():
    page = _page(page_no=46, blocks=[_fig(0.1, 0.2, 0.9, 0.7)])
    assert assets.page_asset(page)["title"] == "Diagram on page 46"


def test_small_inline_figures_are_not_assets():
    # A logo / formula image (real corpus measures ≤ 0.09) must not qualify.
    page = _page(blocks=[
        {"kind": "paragraph", "text": "Lots of prose about compensation fees."},
        _fig(0.4, 0.4, 0.55, 0.5),                    # 0.015 of the page
        _fig(0.6, 0.6, 0.7, 0.65),
    ])
    assert assets.page_asset(page) is None


def test_prose_page_without_figures_is_not_an_asset():
    page = _page(blocks=[{"kind": "paragraph", "text": "9.4 Appointment of agents"}])
    assert assets.page_asset(page) is None


def test_scan_doc_reads_cached_pages(tmp_path):
    cfg = SimpleNamespace(storage_root=tmp_path)
    d = tmp_path / "pages_md" / "doc1"
    d.mkdir(parents=True)
    (d / "p_018.json").write_text(json.dumps(_page(18, [
        {"kind": "heading", "text": "ATTACHMENT 4"}, _fig(0.05, 0.1, 0.95, 0.95),
    ])), encoding="utf-8")
    (d / "p_001.json").write_text(json.dumps(_page(1, [
        {"kind": "paragraph", "text": "prose"},
    ])), encoding="utf-8")
    (d / "p_002.json").write_text("{corrupt", encoding="utf-8")
    found = assets.scan_doc(cfg, "doc1")
    assert [a["page_no"] for a in found] == [18]


def test_asset_score_generic_words_match_everything_specific_words_rank():
    from api.rag.tools import score_asset
    cooling = {"title": "ATTACHMENT 4: COOLING WATER SECTION", "doc_title": "Tech Spec"}
    chilled = {"title": "ATTACHMENT 5: CHILLED WATER SECTION", "doc_title": "Tech Spec"}
    # "plant schematic" carries only generic words → both score 0 (all returned).
    assert score_asset(cooling, ["plant", "schematic"]) == score_asset(
        chilled, ["plant", "schematic"]) == 0
    # "cooling water schematic" ranks the cooling page above the chilled page.
    q = ["cooling", "water", "schematic"]
    assert score_asset(cooling, q) > score_asset(chilled, q)
