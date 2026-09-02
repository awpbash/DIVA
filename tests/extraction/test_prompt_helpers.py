"""Unit tests for pipeline/extraction/prompt_helpers.py.

These helpers turn one doc.json block into the multi-line string the
LLM prompt templates emit. Coverage:

  - plain blocks render header + text
  - blocks with section_path get the breadcrumb on the header
  - table blocks with a grid render as a markdown pipe-table
  - table blocks without a grid fall back to flat text
  - empty / missing fields don't crash
"""
from __future__ import annotations

from pipeline.extraction.prompt_helpers import render_block, render_grid


# ---------------------------------------------------------------------------
# render_block
# ---------------------------------------------------------------------------


def test_render_block_plain_paragraph():
    out = render_block({"id": "b0042", "kind": "paragraph", "text": "Hello world"})
    assert out == "[b0042 paragraph]\nHello world"


def test_render_block_includes_section_breadcrumb():
    """A block with section_path should carry the breadcrumb on the header line."""
    out = render_block({
        "id": "b0042", "kind": "paragraph", "text": "Body",
        "section_path": ["Schedule 1", "Equipment"],
    })
    assert "(§Schedule 1 > Equipment)" in out
    assert out.startswith("[b0042 paragraph]")


def test_render_block_empty_section_path_omits_breadcrumb():
    out = render_block({"id": "b0042", "kind": "paragraph", "text": "t",
                        "section_path": []})
    # No section marker
    assert "§" not in out


def test_render_block_table_with_grid_renders_markdown_table():
    """The headline win: a kind=table block with grid renders as a pipe-table."""
    block = {
        "id": "b0662", "kind": "table", "text": "(ignored when grid present)",
        "section_path": ["Schedule 1", "Equipment"],
        "grid": {
            "n_rows": 3, "n_columns": 4,
            "headers": ["Equipment", "Brand", "Quantity", "Model"],
            "rows": [
                ["Chiller - Duty", "Daikin", "2", "200 RT Magnetic bearing"],
                ["Chiller - Standby", "Trane", "1", "200 RT Screw"],
            ],
        },
    }
    out = render_block(block)
    assert "[b0662 table]" in out
    assert "(§Schedule 1 > Equipment)" in out
    assert "| Equipment | Brand | Quantity | Model |" in out
    assert "| --- | --- | --- | --- |" in out
    assert "| Chiller - Duty | Daikin | 2 | 200 RT Magnetic bearing |" in out
    assert "| Chiller - Standby | Trane | 1 | 200 RT Screw |" in out


def test_render_block_table_without_grid_falls_back_to_text():
    """No grid -> flat text body. Old docs that haven't been re-merged
    still render the same way they always did."""
    out = render_block({
        "id": "b0099", "kind": "table",
        "text": "Type Brand Qty",
    })
    assert out == "[b0099 table]\nType Brand Qty"


def test_render_block_missing_fields_dont_crash():
    out = render_block({"id": "b0001"})
    assert out.startswith("[b0001 ?]")


def test_render_block_heading_no_breadcrumb_on_itself_when_path_empty():
    """A heading block at the top of the doc has no parent section_path."""
    out = render_block({"id": "b0001", "kind": "heading", "text": "Title"})
    assert out == "[b0001 heading]\nTitle"


# ---------------------------------------------------------------------------
# render_grid
# ---------------------------------------------------------------------------


def test_render_grid_basic():
    out = render_grid({
        "headers": ["A", "B"],
        "rows": [["1", "2"], ["3", "4"]],
    })
    lines = out.splitlines()
    assert lines[0] == "| A | B |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| 1 | 2 |"
    assert lines[3] == "| 3 | 4 |"


def test_render_grid_pads_short_rows_to_match_header_width():
    """A short row pads with empty cells to keep the table rectangular."""
    out = render_grid({
        "headers": ["A", "B", "C"],
        "rows": [["1"]],            # only 1 cell — pad to 3 columns
    })
    assert "| 1 |  |  |" in out


def test_render_grid_escapes_pipe_in_cell():
    """A literal pipe in a cell must be escaped so it doesn't break the row."""
    out = render_grid({
        "headers": ["Equipment"],
        "rows": [["A | B special"]],
    })
    assert r"A \| B special" in out


def test_render_grid_collapses_newlines_in_cell():
    """Multi-line cell content must collapse so the markdown table stays
    on one row per record."""
    out = render_grid({
        "headers": ["X"],
        "rows": [["line1\nline2"]],
    })
    assert "| line1 line2 |" in out
    assert "\nline2" not in out.replace("\n|", "")  # rough check: no row-mid newline


def test_render_grid_empty_grid_returns_at_least_header_separator():
    """An empty grid still renders syntactically valid markdown (avoids
    template confusion)."""
    out = render_grid({"headers": [], "rows": []})
    # Must contain something — at least one cell + separator
    assert "|" in out
