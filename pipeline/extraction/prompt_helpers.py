"""
prompt_helpers.py — pure helpers exposed to the Jinja prompt env.

These functions turn a ``doc.json`` block dict into prompt-ready strings.
Templates call ``{{ render_block(block) }}`` instead of inlining
``[{{block.id}} {{block.kind}}] {{block.text}}`` so the formatting logic
lives in Python (easy to test, easy to evolve).

The renderers surface two pieces of structure the merge step added but
plain ``{{ block.text }}`` would hide:

  * ``section_path`` — the heading breadcrumb the block sits under, e.g.
    ``["Schedule 1", "Equipment"]``. Rendered after the header line as
    ``§Schedule 1 › Equipment``.

  * ``grid`` — for ``kind="table"`` blocks where merge recovered a row x
    column grid, render a markdown pipe-table the LLM can read instead
    of the flat space-joined cell soup. This is the fix that lets the
    LLM see "Brand: Daikin" instead of "Daikin" floating next to
    "Magnetic bearing".

Both fall back cleanly when their fields are missing — old docs without
section_path / grid still render the same way they did before.
"""
from __future__ import annotations


_BREADCRUMB_SEP = " > "


def render_block(block: dict) -> str:
    """Render one ``doc.json`` block as a multi-line string for prompt insertion.

    Output shape:

        [b0042 paragraph]  (§Schedule 1 > Equipment)
        Body text goes here.

    For ``kind="table"`` blocks that carry a ``grid``, the body is a
    markdown pipe-table:

        [b0662 table]  (§Schedule 1 > Equipment)
        | Equipment       | Brand   | Quantity | Model / Type             |
        | --------------- | ------- | -------- | ------------------------ |
        | Chiller - Duty  | Daikin  | 2        | 200 RT Magnetic bearing  |

    Blocks without section_path skip the breadcrumb. Blocks without a
    grid (the vast majority) render as ``text``.
    """
    bid = str(block.get("id", "?"))
    kind = str(block.get("kind", "?"))

    section_path = block.get("section_path") or []
    grid = block.get("grid")
    text = str(block.get("text", "") or "")

    header = f"[{bid} {kind}]"
    if section_path:
        header += f"  (§{_BREADCRUMB_SEP.join(str(s) for s in section_path)})"

    if grid:
        body = render_grid(grid)
    else:
        body = text

    return f"{header}\n{body}" if body else header


def render_grid(grid: dict) -> str:
    """Render a recovered table grid as a markdown pipe-table.

    Input shape (produced by ``merge._recover_table_grid``):

        {
          "n_rows": 3,
          "n_columns": 4,
          "headers": ["Equipment", "Brand", "Quantity", "Model"],
          "rows":    [["Chiller - Duty", "Daikin", "2", "200 RT ..."],
                      ["Chiller - Standby", "Trane", "1", "200 RT ..."]],
        }

    Empty headers / empty rows are tolerated. Cells are stringified and
    pipe-escaped so a literal ``|`` in a cell doesn't break parsing.
    """
    headers = list(grid.get("headers") or [])
    rows = list(grid.get("rows") or [])

    n_cols = max(
        len(headers),
        max((len(r) for r in rows), default=0),
        1,
    )

    # Pad short rows / headers so every line in the rendered table has
    # the same column count (markdown parsers are forgiving but
    # consistency helps the LLM read).
    def _norm(cells: list) -> list[str]:
        out = [_escape_cell(c) for c in cells]
        while len(out) < n_cols:
            out.append("")
        return out

    h_row = _norm(headers)
    sep_row = ["---"] * n_cols

    lines = [_format_row(h_row), _format_row(sep_row)]
    for r in rows:
        lines.append(_format_row(_norm(r)))
    return "\n".join(lines)


def _format_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _escape_cell(value) -> str:
    """Stringify and pipe-escape one cell. Newlines collapse to spaces so
    a multiline cell can't break the markdown row layout."""
    s = "" if value is None else str(value)
    # Collapse internal whitespace; escape pipes that would break the row
    return s.replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()
