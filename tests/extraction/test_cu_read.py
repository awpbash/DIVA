"""cu_read adapter tests — pure, fixture-driven, no live service.

The contract under test: a CU DocumentContent (GA 2025-11-01 shape) adapts
into the SAME pages_md artifacts the RapidOCR + correct/classify pair
writes, with geometry normalised to [0,1] so evidence highlighting is
extractor-independent.
"""
from __future__ import annotations

import json

import pytest

from pipeline.extraction import cu_read
from pipeline.extraction.merge import _grid_from_cells, merge_pages


# ---------------------------------------------------------------------------
# parse_source
# ---------------------------------------------------------------------------


def test_parse_source_single_region():
    regions = cu_read.parse_source("D(1,1.0,0.5,7.5,0.5,7.5,1.0,1.0,1.0)")
    assert len(regions) == 1
    page, pts = regions[0]
    assert page == 1
    assert pts == [[1.0, 0.5], [7.5, 0.5], [7.5, 1.0], [1.0, 1.0]]


def test_parse_source_multi_region_and_junk():
    s = "D(1,0,0,1,0,1,1,0,1);D(2,0,0,2,0,2,2,0,2)"
    regions = cu_read.parse_source(s)
    assert [r[0] for r in regions] == [1, 2]
    assert cu_read.parse_source("") == []
    assert cu_read.parse_source(None) == []
    assert cu_read.parse_source("D(nonsense)") == []
    # Odd coordinate count = malformed, skipped
    assert cu_read.parse_source("D(1,0,0,1)") == []


# ---------------------------------------------------------------------------
# to_vision_pages — the full adapter
# ---------------------------------------------------------------------------

_MD = ("# SUPPLY AGREEMENT\n\nThe consumption charge is $0.58 per RTh.\n\n"
       "| Item | Rate |\n| --- | --- |\n| CHW | 0.58 |\n\nPage two body.")

_P1_LEN = _MD.index("Page two body.")


def _content() -> dict:
    """Two 8.5x11in pages: a title, a body paragraph, a table (2x2) with an
    overlapping in-table paragraph that must be suppressed, a page footer,
    and a figure on page 2."""
    return {
        "kind": "document",
        "markdown": _MD,
        "unit": "inch",
        "pages": [
            {"pageNumber": 1, "width": 8.5, "height": 11.0,
             "spans": [{"offset": 0, "length": _P1_LEN}]},
            {"pageNumber": 2, "width": 8.5, "height": 11.0,
             "spans": [{"offset": _P1_LEN, "length": len(_MD) - _P1_LEN}]},
        ],
        "paragraphs": [
            {"content": "SUPPLY AGREEMENT", "role": "title",
             "source": "D(1,1.0,0.5,7.5,0.5,7.5,1.0,1.0,1.0)"},
            {"content": "The consumption charge is $0.58 per RTh.",
             "source": "D(1,1.0,2.0,7.0,2.0,7.0,3.0,1.0,3.0)"},
            # Center (4.0, 6.5) falls inside the table region -> suppressed
            {"content": "CHW 0.58",
             "source": "D(1,3.0,6.0,5.0,6.0,5.0,7.0,3.0,7.0)"},
            {"content": "Page 1 of 2", "role": "pageFooter",
             "source": "D(1,3.5,10.5,5.0,10.5,5.0,10.8,3.5,10.8)"},
            {"content": "Page two body.",
             "source": "D(2,1.0,2.0,7.0,2.0,7.0,3.0,1.0,3.0)"},
        ],
        "tables": [
            {"rowCount": 2, "columnCount": 2,
             "source": "D(1,1.0,5.0,7.0,5.0,7.0,8.0,1.0,8.0)",
             "caption": {"content": "Rates table"},
             "cells": [
                 {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader",
                  "content": "Item",
                  "source": "D(1,1.0,5.0,4.0,5.0,4.0,6.5,1.0,6.5)"},
                 {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader",
                  "content": "Rate",
                  "source": "D(1,4.0,5.0,7.0,5.0,7.0,6.5,4.0,6.5)"},
                 {"rowIndex": 1, "columnIndex": 0, "content": "CHW",
                  "source": "D(1,1.0,6.5,4.0,6.5,4.0,8.0,1.0,8.0)"},
                 {"rowIndex": 1, "columnIndex": 1, "content": "0.58",
                  "source": "D(1,4.0,6.5,7.0,6.5,7.0,8.0,4.0,8.0)"},
             ]},
        ],
        "figures": [
            {"id": "2.1", "description": "Site schematic",
             "caption": {"content": "Figure 1: plant room"},
             "source": "D(2,1.0,4.0,7.0,4.0,7.0,9.0,1.0,9.0)"},
        ],
    }


@pytest.fixture()
def pages():
    return cu_read.to_vision_pages(_content())


def test_pages_and_markdown_slicing(pages):
    assert set(pages) == {1, 2}
    assert pages[1].markdown.startswith("# SUPPLY AGREEMENT")
    assert "Page two body." not in pages[1].markdown
    assert pages[2].markdown == "Page two body."


def test_roles_map_to_kinds(pages):
    kinds = {b.text: b.kind for b in pages[1].blocks}
    assert kinds["SUPPLY AGREEMENT"] == "heading"
    assert kinds["The consumption charge is $0.58 per RTh."] == "paragraph"
    assert kinds["Page 1 of 2"] == "footer"


def test_geometry_is_normalised(pages):
    title = next(b for b in pages[1].blocks if b.text == "SUPPLY AGREEMENT")
    assert title.bbox == pytest.approx([1.0 / 8.5, 0.5 / 11, 7.5 / 8.5, 1.0 / 11])
    assert title.polygon is not None
    assert all(0.0 <= v <= 1.0 for pt in title.polygon for v in pt)


def test_in_table_paragraph_suppressed(pages):
    texts = [b.text for b in pages[1].blocks if b.kind == "paragraph"]
    assert "CHW 0.58" not in texts


def test_table_block_and_cells(pages):
    tables = [b for b in pages[1].blocks if b.kind == "table"]
    assert len(tables) == 1
    t = tables[0]
    assert t.table_meta is not None
    assert (t.table_meta.n_rows, t.table_meta.n_columns) == (2, 2)
    assert t.table_meta.caption == "Rates table"
    assert "| Item | Rate |" in t.text

    cells = [b for b in pages[1].blocks if b.kind == "table_cell"]
    assert len(cells) == 4
    by_pos = {(c.table_cell.row_index, c.table_cell.column_index): c for c in cells}
    assert by_pos[(0, 0)].table_cell.cell_role == "columnHeader"
    assert by_pos[(1, 1)].table_cell.cell_role == "data"
    assert by_pos[(1, 1)].text == "0.58"
    assert {c.table_cell.table_id for c in cells} == {tables[0].table_meta.table_id}


def test_figure_block(pages):
    figs = [b for b in pages[2].blocks if b.kind == "figure"]
    assert len(figs) == 1
    assert figs[0].figure.caption == "Figure 1: plant room"
    assert figs[0].text == "Site schematic"


def test_reading_order_sorted(pages):
    ys = [b.bbox[1] for b in pages[1].blocks]
    assert ys == sorted(ys)


# ---------------------------------------------------------------------------
# merge: grid from structured cells (no rapidocr lines needed)
# ---------------------------------------------------------------------------


def _page_json_from(pages, page_no):
    page = pages[page_no]
    return {
        "page_no": page_no,
        "page_size": [8.5, 11.0],
        "markdown": page.markdown,
        "blocks": [b.model_dump(exclude_none=True) for b in page.blocks],
    }


def test_grid_from_cells_via_merge(pages):
    clean, geometry = merge_pages([_page_json_from(pages, 1)])
    table = next(b for p in clean["pages"] for b in p["blocks"]
                 if b.get("kind") == "table")
    assert table["grid"] == {
        "n_rows": 2, "n_columns": 2,
        "headers": ["Item", "Rate"],
        "rows": [["CHW", "0.58"]],
    }
    # Geometry sidecar carries every block, table cells included
    assert all("bbox" in g for g in geometry["blocks"].values())
    assert len(geometry["blocks"]) == 8   # heading, para, footer, table, 4 cells


def test_grid_from_cells_none_without_cells():
    table = {"kind": "table", "table_meta": {"table_id": "t9"}}
    assert _grid_from_cells(table, [table]) is None


def test_pages_md_artifact_shape(pages):
    """The dict written to pages_md must round-trip as JSON and carry the
    fields merge.py reads (page_no, page_size, markdown, blocks)."""
    payload = _page_json_from(pages, 1)
    parsed = json.loads(json.dumps(payload))
    assert parsed["blocks"], "blocks survived serialisation"
    assert {"kind", "text", "bbox"} <= set(parsed["blocks"][0])
