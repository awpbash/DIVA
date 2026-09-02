"""Unit tests for pipeline/extraction/rapidocr_ocr.py.

Stub-runnable: all logic tested against synthetic dicts shaped like
``RapidOCR(...)`` output (``boxes``, ``txts``, ``scores`` arrays). Live
``extract_page`` is touched only for the "raises when missing" check.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from pipeline.extraction.rapidocr_ocr import (
    HEADING_HEIGHT_RATIO,
    LINE_SCORE_THRESHOLD,
    PARAGRAPH_GAP_RATIO,
    _check_rapidocr_available,
    _filter_and_dictify,
    _group_lines_into_paragraphs,
    _median,
    _normalize_bbox,
    _normalize_polygon,
    _paragraph_envelope_polygon,
    _polygon_bbox,
    process_doc,
    to_vision_page,
)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def test_polygon_bbox_returns_envelope():
    poly = [[100, 200], [400, 220], [395, 350], [110, 330]]
    assert _polygon_bbox(poly) == [100, 200, 400, 350]


def test_normalize_polygon_scales_to_unit():
    poly = [[1240, 1754], [2480, 1754], [2480, 3508], [1240, 3508]]
    norm = _normalize_polygon(poly, 2480, 3508)
    assert norm == [[0.5, 0.5], [1.0, 0.5], [1.0, 1.0], [0.5, 1.0]]


def test_normalize_bbox_passthrough_on_zero_dims():
    assert _normalize_bbox([1, 2, 3, 4], 0, 100) == [1, 2, 3, 4]
    assert _normalize_bbox([1, 2, 3, 4], 100, 0) == [1, 2, 3, 4]


def test_median_basic():
    assert _median([1, 2, 3]) == 2
    assert _median([1, 2, 3, 4]) == 2.5
    assert _median([]) == 0.0


# ---------------------------------------------------------------------------
# _filter_and_dictify (confidence + empty text)
# ---------------------------------------------------------------------------


def test_filter_drops_low_confidence_lines():
    boxes = [[[0, 0], [10, 0], [10, 5], [0, 5]],
             [[0, 6], [10, 6], [10, 11], [0, 11]]]
    txts = ["good", "bad"]
    scores = [0.9, 0.1]  # second below LINE_SCORE_THRESHOLD=0.30
    out = _filter_and_dictify(boxes, txts, scores)
    assert len(out) == 1
    assert out[0]["text"] == "good"


def test_filter_drops_empty_text():
    boxes = [[[0, 0], [10, 0], [10, 5], [0, 5]],
             [[0, 6], [10, 6], [10, 11], [0, 11]]]
    txts = ["good", "   "]
    scores = [0.9, 0.9]
    out = _filter_and_dictify(boxes, txts, scores)
    assert len(out) == 1


def test_filter_drops_malformed_box():
    boxes = [[[0, 0], [10, 0], [10, 5]]]   # only 3 corners
    txts = ["a"]
    scores = [0.9]
    out = _filter_and_dictify(boxes, txts, scores)
    assert out == []


def test_filter_preserves_order():
    """Input order matters for downstream reading-order assumptions."""
    boxes = [
        [[0, 0], [10, 0], [10, 5], [0, 5]],
        [[0, 6], [10, 6], [10, 11], [0, 11]],
        [[0, 12], [10, 12], [10, 17], [0, 17]],
    ]
    txts = ["first", "second", "third"]
    scores = [0.9, 0.9, 0.9]
    out = _filter_and_dictify(boxes, txts, scores)
    assert [r["text"] for r in out] == ["first", "second", "third"]


def test_filter_computes_height_from_bbox():
    boxes = [[[0, 100], [50, 100], [50, 140], [0, 140]]]
    out = _filter_and_dictify(boxes, ["text"], [0.9])
    assert out[0]["height"] == 40


# ---------------------------------------------------------------------------
# Paragraph grouping
# ---------------------------------------------------------------------------


def _make_line(*, x0: float, y0: float, x1: float, y1: float,
               text: str = "x", score: float = 0.9) -> dict:
    return {
        "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        "bbox":    [x0, y0, x1, y1],
        "text":    text,
        "score":   score,
        "height":  y1 - y0,
    }


def test_group_consecutive_close_lines_into_one_paragraph():
    """Three lines stacked tightly (gap < 1.2 × avg height) → one paragraph."""
    lines = [
        _make_line(x0=100, y0=100, x1=500, y1=130),
        _make_line(x0=100, y0=135, x1=500, y1=165),   # gap=5, h=30, ratio=0.17 < 1.2
        _make_line(x0=100, y0=170, x1=500, y1=200),
    ]
    paras = _group_lines_into_paragraphs(lines)
    assert len(paras) == 1
    assert len(paras[0]) == 3


def test_group_starts_new_paragraph_on_large_gap():
    """Big vertical gap → new paragraph."""
    lines = [
        _make_line(x0=100, y0=100, x1=500, y1=130),
        _make_line(x0=100, y0=300, x1=500, y1=330),   # gap=170, h=30, ratio=5.7 > 1.2
    ]
    paras = _group_lines_into_paragraphs(lines)
    assert len(paras) == 2


def test_group_sorts_unsorted_input():
    lines = [
        _make_line(x0=100, y0=300, x1=500, y1=330, text="bottom"),
        _make_line(x0=100, y0=100, x1=500, y1=130, text="top"),
    ]
    paras = _group_lines_into_paragraphs(lines)
    # Each becomes its own paragraph due to gap; verify ordering
    assert paras[0][0]["text"] == "top"
    assert paras[1][0]["text"] == "bottom"


def test_group_empty_returns_empty():
    assert _group_lines_into_paragraphs([]) == []


def test_paragraph_envelope_polygon_covers_all_lines():
    lines = [
        _make_line(x0=100, y0=100, x1=500, y1=130),
        _make_line(x0=80,  y0=135, x1=550, y1=165),
        _make_line(x0=120, y0=170, x1=480, y1=200),
    ]
    poly = _paragraph_envelope_polygon(lines)
    assert poly == [[80, 100], [550, 100], [550, 200], [80, 200]]


# ---------------------------------------------------------------------------
# to_vision_page end-to-end
# ---------------------------------------------------------------------------


def _raw_two_paragraphs() -> dict:
    """Synthetic RapidOCR output: a heading line + a two-line body paragraph
    on a 2480×3508 page."""
    return {
        "boxes": [
            # Heading: tall (h=80) at y=200-280
            [[400, 200], [2080, 200], [2080, 280], [400, 280]],
            # Body line 1: normal height (h=40) at y=400-440
            [[300, 400], [1200, 400], [1200, 440], [300, 440]],
            # Body line 2: normal height (h=40) at y=450-490 (gap=10 < 1.2*40=48)
            [[300, 450], [1300, 450], [1300, 490], [300, 490]],
        ],
        "txts":   ["CHILLED WATER AGREEMENT", "This is the first line",
                   "and this is the second line."],
        "scores": [0.95, 0.88, 0.91],
    }


def test_to_vision_page_emits_two_paragraphs():
    page = to_vision_page(_raw_two_paragraphs(), page_size=(2480.0, 3508.0))
    assert len(page.blocks) == 2


def test_to_vision_page_detects_heading_by_font_size():
    page = to_vision_page(_raw_two_paragraphs(), page_size=(2480.0, 3508.0))
    kinds = [b.kind for b in page.blocks]
    assert kinds.count("heading") == 1
    assert kinds.count("paragraph") == 1


def test_to_vision_page_joins_body_lines_into_one_paragraph():
    page = to_vision_page(_raw_two_paragraphs(), page_size=(2480.0, 3508.0))
    body = next(b for b in page.blocks if b.kind == "paragraph")
    assert body.text == "This is the first line and this is the second line."


def test_to_vision_page_polygon_is_normalised():
    page = to_vision_page(_raw_two_paragraphs(), page_size=(2480.0, 3508.0))
    for block in page.blocks:
        assert block.polygon is not None
        for x, y in block.polygon:
            assert 0.0 <= x <= 1.0
            assert 0.0 <= y <= 1.0


def test_to_vision_page_bbox_matches_polygon_envelope():
    page = to_vision_page(_raw_two_paragraphs(), page_size=(2480.0, 3508.0))
    for block in page.blocks:
        assert block.bbox == _polygon_bbox(block.polygon)


def test_to_vision_page_reading_order_top_to_bottom():
    page = to_vision_page(_raw_two_paragraphs(), page_size=(2480.0, 3508.0))
    ys = [b.bbox[1] for b in page.blocks]
    assert ys == sorted(ys)


def test_to_vision_page_markdown_includes_heading_marker():
    page = to_vision_page(_raw_two_paragraphs(), page_size=(2480.0, 3508.0))
    assert "# CHILLED WATER AGREEMENT" in page.markdown


def test_to_vision_page_empty_input():
    page = to_vision_page(
        {"boxes": [], "txts": [], "scores": []},
        page_size=(2000.0, 2000.0),
    )
    assert page.blocks == []
    assert page.markdown == ""


def test_to_vision_page_all_filtered_returns_empty():
    """Every line below confidence → no blocks."""
    raw = {
        "boxes":  [[[0, 0], [10, 0], [10, 10], [0, 10]]],
        "txts":   ["x"],
        "scores": [0.1],   # below LINE_SCORE_THRESHOLD
    }
    page = to_vision_page(raw, page_size=(100.0, 100.0))
    assert page.blocks == []


# ---------------------------------------------------------------------------
# Constants pinned (changing these silently affects extraction quality)
# ---------------------------------------------------------------------------


def test_constants_pinned():
    assert LINE_SCORE_THRESHOLD == 0.30
    assert PARAGRAPH_GAP_RATIO == 1.2
    assert HEADING_HEIGHT_RATIO == 1.4


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------


def test_check_rapidocr_available_returns_tuple():
    ok, msg = _check_rapidocr_available()
    assert isinstance(ok, bool)
    assert isinstance(msg, str)


# ---------------------------------------------------------------------------
# process_doc with seeded raw cache
# ---------------------------------------------------------------------------


@dataclass
class _StubCfg:
    storage_root: Path
    openai_api_key: str = "test"
    vision_model: str = "gpt-5.4-mini"
    text_model: str = "gpt-5.4-mini"
    reasoning_model: str = "gpt-5.4"
    embed_model: str = "text-embedding-3-large"
    neo4j_uri: str = ""
    neo4j_user: str = ""
    neo4j_password: str = ""
    render_dpi: int = 200
    vision_concurrency: int = 4


def test_process_doc_uses_raw_cache_when_present(tmp_path):
    """Pre-seed the per-page raw cache so process_doc adapts without
    invoking RapidOCR."""
    cfg = _StubCfg(storage_root=tmp_path)
    doc_id = "fixture-doc"

    pages_dir = tmp_path / "pages" / doc_id
    pages_dir.mkdir(parents=True)
    (pages_dir / "p_001.png").write_bytes(b"placeholder")

    raw_cache = tmp_path / "rapidocr" / doc_id / "p_001.json"
    raw_cache.parent.mkdir(parents=True)
    raw = _raw_two_paragraphs()
    raw["page_size"] = [2480, 3508]
    raw_cache.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    counts = process_doc(cfg, doc_id, force=False)
    assert counts["pages"] == 1
    assert counts["n_blocks"] == 2

    out = tmp_path / "pages_md_rapidocr" / doc_id / "p_001.json"
    assert out.exists()
    blob = json.loads(out.read_text(encoding="utf-8"))
    assert blob["page_no"] == 1
    assert blob["extractor"] == "rapidocr"
    assert blob["page_size"] == {"w": 2480, "h": 3508}
    kinds = [b["kind"] for b in blob["blocks"]]
    assert "heading" in kinds


def test_process_doc_raises_when_no_renders(tmp_path):
    cfg = _StubCfg(storage_root=tmp_path)
    with pytest.raises(FileNotFoundError, match="rendered pages"):
        process_doc(cfg, "no-such-doc")
