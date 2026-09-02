"""Unit tests for pipeline/extraction/merge.py.

merge_pages is a pure function over a list of per-page JSONs. run() adds
filesystem IO + caching on top.

Contract:
  - merge_pages returns (clean_doc, geometry_sidecar) tuple
  - Clean doc carries per-page blocks with {id, kind, text} only — no
    bbox / polygon (those live in the sidecar keyed by id)
  - Block IDs are stable doc-wide sequential (b0001, b0002, …) in
    (page_no, reading-order) order
  - run() writes both files: storage/doc/<doc>.json + storage/doc_geometry/<doc>.json
  - Source: storage/pages_md/<doc>/ (output of correct_classify.py)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from pipeline.extraction.merge import merge_pages, run, _input_sha


def _make_cfg(tmp_path: Path):
    @dataclass
    class _C:
        storage_root: Path = tmp_path
        openai_api_key: str = ""
        text_model: str = ""
        vision_model: str = ""
        embed_model: str = ""
        render_dpi: int = 200
        vision_concurrency: int = 4
        neo4j_uri: str = ""
        neo4j_user: str = ""
        neo4j_password: str = ""
    return _C()


def _page(n: int, md: str, blocks=None) -> dict:
    return {
        "page_no": n,
        "page_size": {"w": 1000, "h": 1500},
        "markdown": md,
        "blocks": blocks or [],
        "cache": {"png_sha": "x", "prompt_sha": "y"},
    }


def _block(kind: str, text: str, *, y0: float = 0.1, x0: float = 0.1,
           y1: float = 0.2, x1: float = 0.9, polygon=None) -> dict:
    b = {"kind": kind, "text": text, "bbox": [x0, y0, x1, y1]}
    if polygon is not None:
        b["polygon"] = polygon
    return b


# ---------------------------------------------------------------------------
# merge_pages — pure, returns (clean_doc, geometry_sidecar)
# ---------------------------------------------------------------------------


def test_merge_returns_tuple_of_clean_and_geometry():
    clean, geom = merge_pages([_page(1, "x", blocks=[_block("paragraph", "A")])])
    assert isinstance(clean, dict)
    assert isinstance(geom, dict)
    assert "blocks" in geom


def test_merge_assigns_sequential_block_ids():
    page_jsons = [
        _page(1, "A", blocks=[
            _block("heading",   "Title",     y0=0.05, x0=0.1, y1=0.1, x1=0.9),
            _block("paragraph", "Body",      y0=0.15, x0=0.1, y1=0.25, x1=0.9),
        ]),
        _page(2, "B", blocks=[
            _block("paragraph", "Page 2",    y0=0.1,  x0=0.1, y1=0.2, x1=0.9),
        ]),
    ]
    clean, _ = merge_pages(page_jsons)
    ids = [b["id"] for p in clean["pages"] for b in p["blocks"]]
    assert ids == ["b0001", "b0002", "b0003"]


def test_merge_sorts_blocks_within_page_by_reading_order():
    """Blocks emitted out of order should be sorted by (y0, x0) per page."""
    page_jsons = [_page(1, "x", blocks=[
        _block("paragraph", "second",  y0=0.5, x0=0.1),
        _block("heading",   "title",   y0=0.1, x0=0.1),
        _block("paragraph", "third",   y0=0.5, x0=0.6),
    ])]
    clean, _ = merge_pages(page_jsons)
    texts = [b["text"] for b in clean["pages"][0]["blocks"]]
    # Sort key: (y0, x0) → title(0.1) before second(0.5, 0.1) before third(0.5, 0.6)
    assert texts == ["title", "second", "third"]


def test_merge_clean_doc_drops_bbox_from_blocks():
    page_jsons = [_page(1, "x", blocks=[_block("paragraph", "A")])]
    clean, _ = merge_pages(page_jsons)
    block = clean["pages"][0]["blocks"][0]
    assert "id" in block
    assert "kind" in block
    assert "text" in block
    assert "bbox" not in block       # bbox lives in the sidecar
    assert "polygon" not in block


def test_merge_geometry_sidecar_keyed_by_block_id():
    page_jsons = [_page(1, "x", blocks=[
        _block("paragraph", "A", y0=0.1, x0=0.1, y1=0.2, x1=0.9),
    ])]
    _, geom = merge_pages(page_jsons)
    assert "b0001" in geom["blocks"]
    entry = geom["blocks"]["b0001"]
    assert entry["page_no"] == 1
    assert entry["bbox"] == [0.1, 0.1, 0.9, 0.2]


def test_merge_geometry_carries_polygon_when_present():
    polygon = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.2], [0.1, 0.2]]
    page_jsons = [_page(1, "x", blocks=[
        _block("paragraph", "A", polygon=polygon),
    ])]
    _, geom = merge_pages(page_jsons)
    assert geom["blocks"]["b0001"]["polygon"] == polygon


def test_merge_preserves_table_meta_and_table_cell_on_clean_doc():
    """Structural metadata is small + useful for the LLM — keep on clean doc."""
    block = {
        "kind": "table_cell", "text": "X", "bbox": [0.1, 0.1, 0.2, 0.2],
        "table_cell": {"table_id": "t001", "row_index": 0, "column_index": 0,
                        "row_span": 1, "column_span": 1, "cell_role": "data"},
    }
    page_jsons = [_page(1, "x", blocks=[block])]
    clean, _ = merge_pages(page_jsons)
    cb = clean["pages"][0]["blocks"][0]
    assert cb["table_cell"]["row_index"] == 0


def test_merge_sorts_by_page_no():
    clean, _ = merge_pages([_page(3, "C"), _page(1, "A"), _page(2, "B")])
    assert [p["page_no"] for p in clean["pages"]] == [1, 2, 3]


def test_merge_drops_per_page_cache_block():
    """Each page's local cache info is an artifact of vision.py, not merged in."""
    clean, _ = merge_pages([_page(1, "x")])
    assert "cache" not in clean["pages"][0]


def test_merge_emits_n_pages():
    clean, _ = merge_pages([_page(1, "x"), _page(2, "y"), _page(5, "z")])
    assert clean["n_pages"] == 3


def test_merge_empty_input():
    clean, geom = merge_pages([])
    assert clean == {"n_pages": 0, "pages": []}
    assert geom == {"blocks": {}}


def test_merge_string_page_no_is_coerced_to_int():
    clean, _ = merge_pages([
        {"page_no": "2", "markdown": "B"},
        {"page_no": "1", "markdown": "A"},
    ])
    assert [p["page_no"] for p in clean["pages"]] == [1, 2]


# ---------------------------------------------------------------------------
# Section walk — heading hierarchy -> section_path on every non-heading block
# ---------------------------------------------------------------------------


def test_section_path_attached_under_single_heading():
    """Body block following a heading should carry the heading as section_path."""
    page_jsons = [_page(1, "x", blocks=[
        _block("heading",   "Definitions", y0=0.05, y1=0.10),
        _block("paragraph", "In this Agreement...", y0=0.12, y1=0.14),
    ])]
    clean, _ = merge_pages(page_jsons)
    para = clean["pages"][0]["blocks"][1]
    assert para["text"] == "In this Agreement..."
    assert para["section_path"] == ["Definitions"]


def test_section_path_persists_across_pages():
    """A heading on page 1 should still cover blocks on page 2 until the
    next heading appears."""
    page_jsons = [
        _page(1, "x", blocks=[
            _block("heading",   "Term", y0=0.05, y1=0.10),
            _block("paragraph", "p1",   y0=0.12, y1=0.14),
        ]),
        _page(2, "y", blocks=[
            _block("paragraph", "p2",   y0=0.05, y1=0.07),
        ]),
    ]
    clean, _ = merge_pages(page_jsons)
    page2_para = clean["pages"][1]["blocks"][0]
    assert page2_para["section_path"] == ["Term"]


def test_section_path_hierarchy_by_heading_height():
    """Taller heading is level 1; shorter heading nested under it is level 2.
    Body block under both gets a 2-element section_path."""
    page_jsons = [_page(1, "x", blocks=[
        # Big h1 — height = 0.05
        _block("heading",   "Part A", y0=0.02, y1=0.07),
        # Smaller h2 — height = 0.025
        _block("heading",   "Schedule 1", y0=0.10, y1=0.125),
        _block("paragraph", "body",   y0=0.13, y1=0.15),
    ])]
    clean, _ = merge_pages(page_jsons)
    para = clean["pages"][0]["blocks"][2]
    assert para["section_path"] == ["Part A", "Schedule 1"]


def test_section_path_sibling_heading_replaces_at_same_level():
    """Two same-height headings should be siblings, not nest. Body after
    the second one carries only the second heading."""
    page_jsons = [_page(1, "x", blocks=[
        _block("heading",   "A", y0=0.02, y1=0.06),
        _block("paragraph", "body-a", y0=0.08, y1=0.10),
        _block("heading",   "B", y0=0.20, y1=0.24),
        _block("paragraph", "body-b", y0=0.26, y1=0.28),
    ])]
    clean, _ = merge_pages(page_jsons)
    body_b = clean["pages"][0]["blocks"][3]
    assert body_b["section_path"] == ["B"]


def test_section_path_deeper_heading_clears_under_shallower():
    """When a shallower (taller) heading appears, deeper levels in the
    stack reset — the next body shouldn't keep stale deep labels."""
    page_jsons = [_page(1, "x", blocks=[
        _block("heading",   "Big",     y0=0.02, y1=0.08),     # level 1
        _block("heading",   "Small",   y0=0.10, y1=0.13),     # level 2
        _block("paragraph", "under-small", y0=0.14, y1=0.16),
        _block("heading",   "Big2",    y0=0.20, y1=0.26),     # level 1 again
        _block("paragraph", "under-big2", y0=0.28, y1=0.30),
    ])]
    clean, _ = merge_pages(page_jsons)
    under_big2 = clean["pages"][0]["blocks"][4]
    # Only "Big2" — the previous "Small" must be cleared.
    assert under_big2["section_path"] == ["Big2"]


def test_section_walk_skips_overlong_heading_as_section_anchor():
    """When correct_classify mis-labels a long body paragraph as a heading,
    don't push it onto the section stack — body blocks under it should
    keep the *previous* real heading as their breadcrumb."""
    long_text = "A" * 200
    page_jsons = [_page(1, "x", blocks=[
        _block("heading",   "Real Section", y0=0.05, y1=0.10),
        _block("paragraph", "real body",    y0=0.12, y1=0.14),
        _block("heading",   long_text,      y0=0.20, y1=0.22),
        _block("paragraph", "still real",   y0=0.25, y1=0.27),
    ])]
    clean, _ = merge_pages(page_jsons)
    # The paragraph after the bogus heading must still cite "Real Section"
    body_after_bogus = clean["pages"][0]["blocks"][3]
    assert body_after_bogus["section_path"] == ["Real Section"]


def test_section_path_absent_when_no_preceding_heading():
    """A block with no heading before it doesn't get a section_path."""
    page_jsons = [_page(1, "x", blocks=[_block("paragraph", "loose")])]
    clean, _ = merge_pages(page_jsons)
    assert "section_path" not in clean["pages"][0]["blocks"][0]


def test_section_walk_does_not_leak_height_helper():
    """Internal _h helper field must not appear on output blocks."""
    page_jsons = [_page(1, "x", blocks=[
        _block("heading", "H", y0=0.05, y1=0.10),
        _block("paragraph", "p"),
    ])]
    clean, _ = merge_pages(page_jsons)
    for b in clean["pages"][0]["blocks"]:
        assert "_h" not in b


# ---------------------------------------------------------------------------
# Table pivot — kind="table" blocks get a grid when rapidocr is supplied
# ---------------------------------------------------------------------------


def _rapid_lines(*, page_size=(1000, 1500), rows: list[list[tuple[float, float, str]]]):
    """Build a synthetic rapidocr cache dict from rows of (x_center, y, text).
    Each line gets a small bounding polygon around its center."""
    boxes = []
    txts = []
    half_h = 15.0  # pixel half-height per line
    half_w_default = 40.0
    for row in rows:
        for (x, y, text) in row:
            w = max(half_w_default, len(text) * 6)
            box = [
                [x - w, y - half_h],
                [x + w, y - half_h],
                [x + w, y + half_h],
                [x - w, y + half_h],
            ]
            boxes.append(box)
            txts.append(text)
    return {"boxes": boxes, "txts": txts,
            "scores": [1.0] * len(txts),
            "page_size": list(page_size)}


def test_table_grid_recovered_from_rapidocr_lines():
    """A kind=table block with rapidocr lines inside it gets a grid view."""
    # Table occupies a normalised box of (0.1, 0.1) -> (0.9, 0.4) on a
    # 1000x1500 page. So lines must sit at pixel rows ~150..600 and
    # pixel cols ~100..900.
    table_bbox = [0.1, 0.1, 0.9, 0.4]
    page = _page(1, "x", blocks=[_block(
        "table", "Type Brand Qty Chiller Daikin 2",
        x0=table_bbox[0], y0=table_bbox[1], x1=table_bbox[2], y1=table_bbox[3],
    )])
    rapid = _rapid_lines(rows=[
        [(200.0, 200.0, "Type"),   (500.0, 200.0, "Brand"),   (800.0, 200.0, "Qty")],
        [(200.0, 300.0, "Chiller"),(500.0, 300.0, "Daikin"),  (800.0, 300.0, "2")],
        [(200.0, 400.0, "Pump"),   (500.0, 400.0, "Grundfos"),(800.0, 400.0, "4")],
    ])
    clean, _ = merge_pages([page], rapidocr_pages={1: rapid})
    table_block = clean["pages"][0]["blocks"][0]
    assert "grid" in table_block
    grid = table_block["grid"]
    assert grid["n_columns"] == 3
    assert grid["headers"] == ["Type", "Brand", "Qty"]
    # Body rows (the header row is split out).
    assert grid["rows"][0] == ["Chiller", "Daikin", "2"]
    assert grid["rows"][1] == ["Pump", "Grundfos", "4"]


def test_table_grid_not_added_without_rapidocr():
    """Without a rapidocr cache, kind=table blocks stay as-is (no grid)."""
    page = _page(1, "x", blocks=[_block("table", "Type Brand Qty")])
    clean, _ = merge_pages([page])
    table_block = clean["pages"][0]["blocks"][0]
    assert "grid" not in table_block


def test_table_grid_not_added_to_non_table_blocks():
    """Paragraphs are never given grids even when rapidocr is supplied."""
    page = _page(1, "x", blocks=[_block("paragraph", "free text",
                                         x0=0.1, y0=0.1, x1=0.9, y1=0.4)])
    rapid = _rapid_lines(rows=[[(200.0, 200.0, "hi"), (400.0, 200.0, "there")]])
    clean, _ = merge_pages([page], rapidocr_pages={1: rapid})
    assert "grid" not in clean["pages"][0]["blocks"][0]


def test_table_grid_skipped_when_too_few_lines():
    """A near-empty table block doesn't get a grid (avoids noisy 1-row 1-col output)."""
    page = _page(1, "x", blocks=[_block(
        "table", "only one line", x0=0.1, y0=0.1, x1=0.9, y1=0.4,
    )])
    rapid = _rapid_lines(rows=[[(200.0, 200.0, "only one line")]])
    clean, _ = merge_pages([page], rapidocr_pages={1: rapid})
    assert "grid" not in clean["pages"][0]["blocks"][0]


# ---------------------------------------------------------------------------
# _input_sha — stable across reorderings
# ---------------------------------------------------------------------------


def test_input_sha_stable_across_input_order():
    a = [_page(2, "B"), _page(1, "A")]
    b = [_page(1, "A"), _page(2, "B")]
    assert _input_sha(a) == _input_sha(b)


def test_input_sha_changes_on_markdown_edit():
    a = [_page(1, "version 1")]
    b = [_page(1, "version 2")]
    assert _input_sha(a) != _input_sha(b)


def test_input_sha_changes_when_rapidocr_changes():
    """The cache key must invalidate if the rapidocr lines we used for
    grid pivot changed underneath us."""
    page = _page(1, "x", blocks=[_block("table", "T")])
    rapid_a = _rapid_lines(rows=[[(200.0, 200.0, "A")]])
    rapid_b = _rapid_lines(rows=[[(200.0, 200.0, "DIFFERENT")]])
    assert _input_sha([page], rapidocr_pages={1: rapid_a}) != \
           _input_sha([page], rapidocr_pages={1: rapid_b})


# ---------------------------------------------------------------------------
# run — filesystem IO + caching + source selection
# ---------------------------------------------------------------------------


def _write_page(tmp_path: Path, doc_id: str, n: int, md: str,
                blocks=None) -> None:
    p = tmp_path / "pages_md" / doc_id / f"p_{n:03d}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_page(n, md, blocks=blocks)), encoding="utf-8")


def test_run_missing_pages_md_raises(tmp_path):
    cfg = _make_cfg(tmp_path)
    with pytest.raises(FileNotFoundError):
        run(cfg, "nope")


def test_run_writes_both_doc_and_geometry_files(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write_page(tmp_path, "d1", 1, "first",
                blocks=[_block("heading", "T")])
    r = run(cfg, "d1")
    assert r.n_pages == 1
    assert r.n_blocks == 1

    doc_path = tmp_path / "doc" / "d1.json"
    geom_path = tmp_path / "doc_geometry" / "d1.json"
    assert doc_path.exists()
    assert geom_path.exists()

    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    geom = json.loads(geom_path.read_text(encoding="utf-8"))
    assert doc["doc_id"] == "d1"
    assert doc["pages"][0]["blocks"][0]["id"] == "b0001"
    assert "b0001" in geom["blocks"]


def test_run_emits_ordered_doc_json(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write_page(tmp_path, "d4", 3, "third")
    _write_page(tmp_path, "d4", 1, "first")
    _write_page(tmp_path, "d4", 2, "second")
    r = run(cfg, "d4")
    assert r.n_pages == 3
    doc = json.loads((tmp_path / "doc" / "d4.json").read_text(encoding="utf-8"))
    assert [p["page_no"] for p in doc["pages"]] == [1, 2, 3]
    assert doc["pages"][0]["markdown"] == "first"


def test_run_is_idempotent(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write_page(tmp_path, "d5", 1, "X")
    run(cfg, "d5")
    mt1 = (tmp_path / "doc" / "d5.json").stat().st_mtime_ns
    r2 = run(cfg, "d5")
    mt2 = (tmp_path / "doc" / "d5.json").stat().st_mtime_ns
    assert r2.cached is True
    assert mt1 == mt2


def test_run_force_rewrites_even_on_no_change(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write_page(tmp_path, "d6", 1, "X")
    r1 = run(cfg, "d6")
    r2 = run(cfg, "d6", force=True)
    assert r1.cached is False
    assert r2.cached is False


def test_run_stale_geometry_sha_forces_rebuild(tmp_path):
    """A geometry sidecar whose input_sha does not match the current inputs
    must NOT count as a cache hit even when doc.json matches (a kill between
    the two writes would otherwise freeze stale bboxes forever)."""
    cfg = _make_cfg(tmp_path)
    _write_page(tmp_path, "d7", 1, "X", blocks=[_block("heading", "T")])
    run(cfg, "d7")

    geom_path = tmp_path / "doc_geometry" / "d7.json"
    geom = json.loads(geom_path.read_text(encoding="utf-8"))
    geom["cache"]["input_sha"] = "stale"
    geom_path.write_text(json.dumps(geom), encoding="utf-8")

    r = run(cfg, "d7")
    assert r.cached is False
    healed = json.loads(geom_path.read_text(encoding="utf-8"))
    assert healed["cache"]["input_sha"] == r.input_sha
