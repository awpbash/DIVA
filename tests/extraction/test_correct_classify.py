"""Unit tests for pipeline/extraction/correct_classify.py.

Stub-runnable: all logic tested without OpenAI calls. process_page uses
a fake LLM. Geometry helpers + apply_corrections + assemble_blocks are
covered as pure functions.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from pipeline.extraction.correct_classify import (
    EffectiveLine,
    _gap_bbox,
    _normalize_bbox,
    _normalize_polygon,
    _ocr_lines_to_effective,
    _polygon_bbox,
    _union_bbox,
    _union_polygon,
    apply_corrections,
    assemble_blocks,
    process_page,
)


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------


def test_polygon_bbox():
    assert _polygon_bbox([[0.1, 0.2], [0.4, 0.2], [0.4, 0.5], [0.1, 0.5]]) == [0.1, 0.2, 0.4, 0.5]


def test_normalize_bbox():
    assert _normalize_bbox([1240, 1754, 2480, 3508], 2480, 3508) == [0.5, 0.5, 1.0, 1.0]


def test_normalize_polygon():
    out = _normalize_polygon([[1240, 1754], [2480, 1754], [2480, 3508], [1240, 3508]],
                             2480, 3508)
    assert out == [[0.5, 0.5], [1.0, 0.5], [1.0, 1.0], [0.5, 1.0]]


# ---------------------------------------------------------------------------
# _ocr_lines_to_effective
# ---------------------------------------------------------------------------


def _raw_rapid(boxes_txts_scores) -> dict:
    boxes = [t[0] for t in boxes_txts_scores]
    txts = [t[1] for t in boxes_txts_scores]
    scores = [t[2] for t in boxes_txts_scores]
    return {
        "boxes": boxes, "txts": txts, "scores": scores,
        "page_size": [1000, 1500],
    }


def test_ocr_lines_filters_low_confidence():
    raw = _raw_rapid([
        ([[0, 0], [100, 0], [100, 30], [0, 30]], "good", 0.9),
        ([[0, 40], [100, 40], [100, 70], [0, 70]], "bad", 0.1),
    ])
    out = _ocr_lines_to_effective(raw)
    assert len(out) == 1
    assert out[0].text == "good"


def test_ocr_lines_drops_empty_text():
    raw = _raw_rapid([
        ([[0, 0], [100, 0], [100, 30], [0, 30]], "  ", 0.9),
        ([[0, 40], [100, 40], [100, 70], [0, 70]], "keep", 0.9),
    ])
    out = _ocr_lines_to_effective(raw)
    assert [L.text for L in out] == ["keep"]


def test_ocr_lines_normalises_geometry():
    raw = _raw_rapid([
        ([[100, 150], [900, 150], [900, 200], [100, 200]], "x", 0.9),
    ])
    out = _ocr_lines_to_effective(raw)
    assert out[0].bbox == pytest.approx([0.1, 0.1, 0.9, 200/1500])
    assert out[0].polygon is not None
    # All polygon coords within [0,1]
    for x, y in out[0].polygon:
        assert 0.0 <= x <= 1.0
        assert 0.0 <= y <= 1.0


def test_ocr_lines_preserves_original_line_id():
    raw = _raw_rapid([
        ([[0, 0], [100, 0], [100, 30], [0, 30]], "a", 0.9),
        ([[0, 100], [100, 100], [100, 130], [0, 130]], "b", 0.05),   # filtered
        ([[0, 200], [100, 200], [100, 230], [0, 230]], "c", 0.9),
    ])
    out = _ocr_lines_to_effective(raw)
    # Original line ids preserved (0 and 2; 1 was filtered out)
    assert [L.original_line_id for L in out] == [0, 2]


# ---------------------------------------------------------------------------
# _gap_bbox
# ---------------------------------------------------------------------------


def _line(y0: float, y1: float, x0: float = 0.1, x1: float = 0.9) -> EffectiveLine:
    return EffectiveLine(
        text="x", raw_text="x", bbox=[x0, y0, x1, y1],
        polygon=[[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        source="ocr", original_line_id=0, score=0.9,
    )


def test_gap_bbox_between_two_lines():
    prev = _line(0.10, 0.15, x0=0.1, x1=0.7)
    nxt = _line(0.25, 0.30, x0=0.2, x1=0.9)
    gap = _gap_bbox(prev, nxt)
    # Vertically, the gap is between prev.bottom (0.15) and nxt.top (0.25).
    # Horizontally, the union of x-extents (0.1..0.9).
    assert gap == [0.1, 0.15, 0.9, 0.25]


def test_gap_bbox_top_of_page():
    nxt = _line(0.10, 0.15)
    gap = _gap_bbox(None, nxt)
    # Strip immediately above nxt
    assert gap[3] == pytest.approx(0.10)
    assert gap[1] < 0.10
    assert gap[1] >= 0.0


def test_gap_bbox_bottom_of_page():
    prev = _line(0.90, 0.95)
    gap = _gap_bbox(prev, None)
    assert gap[1] == pytest.approx(0.95)
    assert gap[3] > 0.95


def test_gap_bbox_neither_neighbour():
    gap = _gap_bbox(None, None)
    assert 0 <= gap[0] <= gap[2] <= 1
    assert 0 <= gap[1] <= gap[3] <= 1


def test_gap_bbox_overlapping_neighbours_emits_thin_strip():
    """When prev.bottom > nxt.top (overlapping lines), don't return an
    inverted bbox — fall back to a thin strip after prev."""
    prev = _line(0.10, 0.20)
    nxt = _line(0.15, 0.25)        # overlaps prev
    gap = _gap_bbox(prev, nxt)
    assert gap[1] == pytest.approx(0.20)
    assert gap[3] > 0.20            # not inverted


# ---------------------------------------------------------------------------
# apply_corrections
# ---------------------------------------------------------------------------


def _make_ocr_lines(n: int) -> list[EffectiveLine]:
    lines = []
    for i in range(n):
        y0 = 0.1 + i * 0.05
        y1 = y0 + 0.04
        lines.append(EffectiveLine(
            text=f"line {i}",
            raw_text=f"line {i}",
            bbox=[0.1, y0, 0.9, y1],
            polygon=[[0.1, y0], [0.9, y0], [0.9, y1], [0.1, y1]],
            source="ocr",
            original_line_id=i,
            score=0.9,
        ))
    return lines


def test_apply_corrections_replaces_text_and_marks_source():
    """A small, plausible cleanup is ACCEPTED: text replaced, source flips
    to ocr_corrected, but raw_text keeps the verbatim OCR anchor."""
    lines = _make_ocr_lines(3)
    out = apply_corrections(
        lines,
        corrections=[{"line_id": 1, "corrected_text": "Line 1"}],  # 1-char fix
        missed=[],
    )
    assert len(out) == 3
    assert out[1].text == "Line 1"
    assert out[1].source == "ocr_corrected"
    assert out[1].raw_text == "line 1"          # anchor never mutated
    # Other lines unchanged
    assert out[0].text == "line 0" and out[0].source == "ocr"
    assert out[2].text == "line 2" and out[2].source == "ocr"


def test_apply_corrections_skips_unknown_line_id():
    lines = _make_ocr_lines(2)
    out = apply_corrections(
        lines,
        corrections=[{"line_id": 99, "corrected_text": "ghost"}],
        missed=[],
    )
    assert all(L.text.startswith("line ") for L in out)


def test_apply_corrections_inserts_missed_after_anchor():
    lines = _make_ocr_lines(3)
    out = apply_corrections(
        lines,
        corrections=[],
        missed=[{"insert_after_line_id": 1, "text": "INSERTED"}],
    )
    assert [L.text for L in out] == ["line 0", "line 1", "INSERTED", "line 2"]
    inserted = out[2]
    assert inserted.source == "llm_missed"
    assert inserted.original_line_id is None
    # Bbox is between line 1 (y1=0.19) and line 2 (y0=0.20)
    assert inserted.bbox[1] == pytest.approx(0.19)


def test_apply_corrections_inserts_at_top():
    lines = _make_ocr_lines(2)
    out = apply_corrections(
        lines, corrections=[],
        missed=[{"insert_after_line_id": -1, "text": "TOP"}],
    )
    assert [L.text for L in out] == ["TOP", "line 0", "line 1"]


def test_apply_corrections_inserts_at_bottom():
    lines = _make_ocr_lines(2)
    out = apply_corrections(
        lines, corrections=[],
        missed=[{"insert_after_line_id": 1, "text": "BOTTOM"}],
    )
    assert [L.text for L in out] == ["line 0", "line 1", "BOTTOM"]


def test_apply_corrections_multiple_missed_at_same_anchor_preserves_order():
    lines = _make_ocr_lines(2)
    out = apply_corrections(
        lines, corrections=[],
        missed=[
            {"insert_after_line_id": 0, "text": "FIRST"},
            {"insert_after_line_id": 0, "text": "SECOND"},
        ],
    )
    assert [L.text for L in out] == ["line 0", "FIRST", "SECOND", "line 1"]


def test_apply_corrections_skips_malformed_entries():
    """Missed without text, correction without line_id — should be dropped."""
    lines = _make_ocr_lines(2)
    out = apply_corrections(
        lines,
        corrections=[{"corrected_text": "no_id"}],
        missed=[{"insert_after_line_id": 0}],
    )
    assert [L.text for L in out] == ["line 0", "line 1"]


# ---------------------------------------------------------------------------
# Correction guard — the verbatim anchor
# ---------------------------------------------------------------------------


def _one_line(text: str, score: float, line_id: int = 0) -> EffectiveLine:
    return EffectiveLine(
        text=text, raw_text=text, bbox=[0.1, 0.1, 0.9, 0.15],
        polygon=[[0.1, 0.1], [0.9, 0.1], [0.9, 0.15], [0.1, 0.15]],
        source="ocr", original_line_id=line_id, score=score,
    )


def test_guard_rejects_wholesale_rewrite():
    """edit_ratio > 0.40 → the LLM is rewriting, not cleaning OCR. Keep raw."""
    lines = [_one_line("line 1", score=0.9)]
    out = apply_corrections(
        lines,
        corrections=[{"line_id": 0, "corrected_text": "a completely different sentence"}],
        missed=[],
    )
    assert out[0].text == "line 1"          # raw OCR retained
    assert out[0].raw_text == "line 1"
    assert out[0].source == "ocr_flagged"


def test_guard_rejects_digit_value_swap_on_confident_line():
    """A same-length digit-for-digit swap (S$500 → S$600) on a confident line
    is an unambiguous value change → refuse, keep the OCR value."""
    lines = [_one_line("S$500", score=0.95)]
    out = apply_corrections(
        lines,
        corrections=[{"line_id": 0, "corrected_text": "S$600"}],
        missed=[],
    )
    assert out[0].text == "S$500"
    assert out[0].source == "ocr_flagged"


def test_guard_accepts_confusable_digit_cleanup():
    """S$5OO → S$500 is a pure glyph cleanup (O→0): the cleaned positions are
    letters on the raw side, so it is NOT a digit swap and is accepted even
    on a high-confidence line."""
    lines = [_one_line("S$5OO", score=0.95)]
    out = apply_corrections(
        lines,
        corrections=[{"line_id": 0, "corrected_text": "S$500"}],
        missed=[],
    )
    assert out[0].text == "S$500"
    assert out[0].source == "ocr_corrected"
    assert out[0].raw_text == "S$5OO"       # anchor still the verbatim OCR


def test_guard_accepts_degluing_around_numbers():
    """The dominant real correction: OCR glues words to numbers, the LLM adds
    spaces. Length changes, so the digit guard never fires → accepted."""
    lines = [_one_line("dated11October2021", score=0.97)]
    out = apply_corrections(
        lines,
        corrections=[{"line_id": 0, "corrected_text": "dated 11 October 2021"}],
        missed=[],
    )
    assert out[0].text == "dated 11 October 2021"
    assert out[0].source == "ocr_corrected"


def test_guard_accepts_digit_swap_on_low_confidence_line():
    """When OCR was unsure (score < 0.90), the LLM's read is trusted — the
    digit guard only protects digits OCR was confident about."""
    lines = [_one_line("S$500", score=0.40)]
    out = apply_corrections(
        lines,
        corrections=[{"line_id": 0, "corrected_text": "S$600"}],
        missed=[],
    )
    assert out[0].text == "S$600"
    assert out[0].source == "ocr_corrected"


def test_guard_noop_correction_keeps_ocr_source():
    """A correction identical to the OCR text is a no-op, not a 'correction'."""
    lines = [_one_line("line 1", score=0.9)]
    out = apply_corrections(
        lines,
        corrections=[{"line_id": 0, "corrected_text": "line 1"}],
        missed=[],
    )
    assert out[0].text == "line 1"
    assert out[0].source == "ocr"


def test_guard_plain_word_fix_not_treated_as_digit_change():
    """A plain-word cleanup with no real digits must not be rejected by the
    digit rule (the confusable map must not manufacture phantom digits)."""
    lines = [_one_line("Suppler", score=0.95)]
    out = apply_corrections(
        lines,
        corrections=[{"line_id": 0, "corrected_text": "Supplier"}],
        missed=[],
    )
    assert out[0].text == "Supplier"
    assert out[0].source == "ocr_corrected"


def test_missed_line_is_low_confidence_and_unanchored():
    """LLM-injected missed lines carry score 0.5 and no OCR anchor."""
    lines = _make_ocr_lines(1)
    out = apply_corrections(
        lines, corrections=[],
        missed=[{"insert_after_line_id": 0, "text": "INJECTED"}],
    )
    injected = out[1]
    assert injected.source == "llm_missed"
    assert injected.score == 0.5
    assert injected.raw_text == ""


# ---------------------------------------------------------------------------
# Union helpers + assemble_blocks
# ---------------------------------------------------------------------------


def test_union_bbox_empty_input():
    assert _union_bbox([]) == [0.0, 0.0, 0.0, 0.0]


def test_union_bbox_multiple():
    bboxes = [[0.1, 0.2, 0.3, 0.4], [0.05, 0.15, 0.5, 0.5]]
    assert _union_bbox(bboxes) == [0.05, 0.15, 0.5, 0.5]


def test_union_polygon_empty_input_returns_empty():
    assert _union_polygon([]) == []


def test_union_polygon_envelope():
    poly1 = [[0.1, 0.2], [0.3, 0.2], [0.3, 0.4], [0.1, 0.4]]
    poly2 = [[0.05, 0.3], [0.5, 0.3], [0.5, 0.5], [0.05, 0.5]]
    assert _union_polygon([poly1, poly2]) == [
        [0.05, 0.2], [0.5, 0.2], [0.5, 0.5], [0.05, 0.5],
    ]


def test_assemble_blocks_groups_lines():
    effective = _make_ocr_lines(4)
    classified = [
        {"kind": "heading", "line_ids": [0]},
        {"kind": "paragraph", "line_ids": [1, 2, 3]},
    ]
    blocks = assemble_blocks(effective, classified)
    assert len(blocks) == 2
    assert blocks[0].kind == "heading"
    assert blocks[0].text == "line 0"
    assert blocks[1].kind == "paragraph"
    assert blocks[1].text == "line 1 line 2 line 3"
    # Paragraph bbox unions lines 1..3
    expected_bbox = _union_bbox([effective[i].bbox for i in (1, 2, 3)])
    assert blocks[1].bbox == expected_bbox


def test_assemble_blocks_orphan_lines_become_their_own_blocks():
    effective = _make_ocr_lines(3)
    classified = [{"kind": "heading", "line_ids": [0]}]
    # Lines 1 and 2 unassigned → each becomes a fallback paragraph block
    blocks = assemble_blocks(effective, classified)
    kinds = [b.kind for b in blocks]
    assert kinds.count("heading") == 1
    assert kinds.count("paragraph") == 2


def test_assemble_blocks_duplicate_line_id_kept_in_first_block():
    """If line 1 appears in two blocks, only the first wins."""
    effective = _make_ocr_lines(2)
    classified = [
        {"kind": "heading", "line_ids": [0, 1]},
        {"kind": "paragraph", "line_ids": [1]},   # line 1 already taken
    ]
    blocks = assemble_blocks(effective, classified)
    assert len(blocks) == 1
    assert blocks[0].kind == "heading"


def test_assemble_blocks_drops_out_of_range_line_ids():
    effective = _make_ocr_lines(2)
    classified = [{"kind": "heading", "line_ids": [0, 5, 99]}]
    blocks = assemble_blocks(effective, classified)
    # Line 0 valid, 5/99 dropped; orphan line 1 becomes fallback
    assert len(blocks) == 2
    heading = next(b for b in blocks if b.kind == "heading")
    assert heading.text == "line 0"


def test_assemble_blocks_empty_classified_makes_all_orphans():
    effective = _make_ocr_lines(3)
    blocks = assemble_blocks(effective, [])
    assert len(blocks) == 3
    assert all(b.kind == "paragraph" for b in blocks)


def test_assemble_blocks_reading_order_by_y():
    """Blocks emitted in top-to-bottom order even if input classified
    them out of sequence."""
    effective = _make_ocr_lines(3)
    classified = [
        {"kind": "paragraph", "line_ids": [2]},
        {"kind": "heading",   "line_ids": [0]},
        {"kind": "paragraph", "line_ids": [1]},
    ]
    blocks = assemble_blocks(effective, classified)
    ys = [b.bbox[1] for b in blocks]
    assert ys == sorted(ys)


# ---------------------------------------------------------------------------
# process_page (end-to-end against a fake LLM)
# ---------------------------------------------------------------------------


@dataclass
class _StubCfg:
    storage_root: Path
    openai_api_key: str = "test"
    vision_model: str = "gpt-test"
    text_model: str = "gpt-5.4-mini"
    reasoning_model: str = "gpt-5.4"
    embed_model: str = "text-embedding-3-large"
    neo4j_uri: str = ""
    neo4j_user: str = ""
    neo4j_password: str = ""
    render_dpi: int = 200
    vision_concurrency: int = 4


def _write_png(path: Path) -> None:
    """Tiny but valid PNG (1x1 white pixel)."""
    # PNG signature + minimal IHDR + IDAT + IEND
    payload = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
        b"\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\xfc\xff\xff?\x00\x05\xfe\x02\xfe"
        b"\xdc\xccY\xe7"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    path.write_bytes(payload)


def test_process_page_end_to_end(tmp_path):
    """Full integration: feed RapidOCR data + a fake LLM, get back
    VisionPageResponse with corrections and blocks applied."""
    png_path = tmp_path / "p_001.png"
    _write_png(png_path)

    rapid_raw = _raw_rapid([
        ([[100, 200], [900, 200], [900, 230], [100, 230]], "Tite", 0.9),  # typo
        ([[100, 240], [900, 240], [900, 270], [100, 270]], "Body line one", 0.9),
        ([[100, 280], [900, 280], [900, 310], [100, 310]], "Body line two", 0.9),
    ])

    async def fake_llm(*, model, system_prompt, image_data_url, schema):
        assert image_data_url.startswith("data:image/png;base64,")
        return {
            "corrections": [{"line_id": 0, "corrected_text": "Title"}],
            "missed": [],
            "blocks": [
                {"kind": "heading", "line_ids": [0]},
                {"kind": "paragraph", "line_ids": [1, 2]},
            ],
        }

    page, stats = asyncio.run(process_page(
        page_no=1, png_path=png_path, rapid_raw=rapid_raw,
        model="gpt-test", llm=fake_llm, cache_path=None,
    ))
    assert len(page.blocks) == 2
    assert page.blocks[0].kind == "heading"
    assert page.blocks[0].text == "Title"     # correction applied
    assert page.blocks[1].kind == "paragraph"
    assert page.blocks[1].text == "Body line one Body line two"
    assert stats.n_corrections == 1
    assert stats.n_missed == 0
    assert "# Title" in page.markdown


def test_process_page_caches_response(tmp_path):
    png_path = tmp_path / "p_001.png"
    _write_png(png_path)
    cache_path = tmp_path / "cache.json"

    rapid_raw = _raw_rapid([
        ([[100, 200], [900, 200], [900, 230], [100, 230]], "ok", 0.9),
    ])
    calls = []

    async def fake_llm(*, model, system_prompt, image_data_url, schema):
        calls.append(True)
        return {"corrections": [], "missed": [],
                "blocks": [{"kind": "paragraph", "line_ids": [0]}]}

    # First call → LLM hit, cache written
    _, stats = asyncio.run(process_page(
        page_no=1, png_path=png_path, rapid_raw=rapid_raw,
        model="gpt-test", llm=fake_llm, cache_path=cache_path,
    ))
    assert stats.cache_hit is False
    assert len(calls) == 1
    assert cache_path.exists()

    # Second call → cache hit, no LLM
    _, stats2 = asyncio.run(process_page(
        page_no=1, png_path=png_path, rapid_raw=rapid_raw,
        model="gpt-test", llm=fake_llm, cache_path=cache_path,
    ))
    assert stats2.cache_hit is True
    assert len(calls) == 1


def test_process_page_skips_llm_when_no_ocr_lines(tmp_path):
    """Empty pages (RapidOCR found nothing) shouldn't burn LLM tokens."""
    png_path = tmp_path / "p_001.png"
    _write_png(png_path)
    empty_raw = _raw_rapid([])

    async def fake_llm(*, model, system_prompt, image_data_url, schema):
        raise AssertionError("should not be called")

    page, stats = asyncio.run(process_page(
        page_no=1, png_path=png_path, rapid_raw=empty_raw,
        model="gpt-test", llm=fake_llm, cache_path=None,
    ))
    assert page.blocks == []
    assert stats.n_ocr_lines == 0


def test_process_page_rejected_correction_falls_back_to_ocr(tmp_path):
    """A rejected wholesale rewrite surfaces in stats and the block keeps the
    raw OCR text — no silent replacement reaches the page output."""
    png_path = tmp_path / "p_001.png"
    _write_png(png_path)
    rapid_raw = _raw_rapid([
        ([[100, 200], [900, 200], [900, 230], [100, 230]], "Deposit of S$500", 0.95),
    ])

    async def fake_llm(*, model, system_prompt, image_data_url, schema):
        # LLM tries to reassign this line's bbox to unrelated text
        return {
            "corrections": [{"line_id": 0,
                             "corrected_text": "means the security sum payable by the Customer"}],
            "missed": [],
            "blocks": [{"kind": "paragraph", "line_ids": [0]}],
        }

    page, stats = asyncio.run(process_page(
        page_no=1, png_path=png_path, rapid_raw=rapid_raw,
        model="gpt-test", llm=fake_llm, cache_path=None,
    ))
    assert stats.n_corrections == 1
    assert stats.n_corrections_rejected == 1
    assert page.blocks[0].text == "Deposit of S$500"   # OCR value preserved
