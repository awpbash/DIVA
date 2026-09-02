"""
merge.py — per-page JSONs -> clean doc + geometry sidecar.

Step 3 of the pipeline. Pure logic; no LLM, no network.

**Source**: ``storage/pages_md/<doc>/`` (output of correct_classify.py),
optionally also ``storage/rapidocr/<doc>/`` (used to recover table grids).

**Outputs** (split-concerns design — the LLM input never carries coordinates):

    storage/doc/<doc>.json
        Clean doc that becomes the LLM's "what is this document" view.
        Per page: page_no, page_size, markdown, blocks: [{id, kind, text,
        section_path?, grid?, ...}].
        Block IDs are doc-stable (b0001, b0002, ...) so harvest can cite
        them and validate / load can dereference them.

    storage/doc_geometry/<doc>.json
        Sidecar keyed on block_id: {page_no, bbox, polygon, source_lines}.
        Read by validate.py (bbox enrichment) and load.py (EvidenceSpan
        writes). Never reaches the LLM.

**Spatial enrichment in this step:**

    1. *Section walk* — for every non-heading block, attach a
       ``section_path`` (e.g. ``["Part A", "Schedule 1", "Equipment"]``)
       inferred from the heading stack seen so far in reading order.
       Heading level is bucketed from bbox-height percentiles.

    2. *Table grid recovery* — when a ``kind="table"`` block is present
       and the RapidOCR cache is available, cluster the OCR lines that
       fall inside the table's bbox into a row x column grid and attach
       it as ``block["grid"] = {n_rows, n_columns, rows: [[cell,...]]}``.
       The first row is exposed as ``headers`` so downstream prompts can
       render the table as ``header | header | ...``.

**Idempotency**
Cached by SHA over (sorted page numbers + each page's markdown + each
page's blocks + the rapidocr files we pulled in). Re-runs with unchanged
inputs skip the write.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..storage import Paths, atomic_write_json


@dataclass(frozen=True)
class MergeResult:
    doc_id: str
    n_pages: int
    n_blocks: int
    cached: bool
    input_sha: str


# ---------------------------------------------------------------------------
# Input hash (cache key)
# ---------------------------------------------------------------------------


def _input_sha(
    page_jsons: list[dict],
    *,
    rapidocr_pages: dict[int, dict] | None = None,
) -> str:
    """Hash that changes iff any page's content changes.

    Includes:
      - page markdown + blocks (post-correct_classify shape)
      - the rapidocr lines we used for table-grid pivot (if any)
    """
    items = sorted(page_jsons, key=lambda p: int(p["page_no"]))
    rapid_payload = None
    if rapidocr_pages:
        rapid_payload = sorted(
            (
                (int(pn), {"boxes": r.get("boxes"), "txts": r.get("txts"),
                           "page_size": r.get("page_size")})
                for pn, r in rapidocr_pages.items()
            ),
            key=lambda t: t[0],
        )
    payload = json.dumps(
        {
            "pages": [
                {
                    "page_no": int(p["page_no"]),
                    "markdown": p.get("markdown", ""),
                    "blocks": p.get("blocks", []),
                }
                for p in items
            ],
            "rapid": rapid_payload,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Pure merge: page_jsons -> (clean_doc, geometry_sidecar)
# ---------------------------------------------------------------------------


# Properties carried over from a per-page block into the clean doc. Anything
# NOT in this list (bbox, polygon, raw OCR confidence, etc.) stays only on
# the geometry sidecar.
_CLEAN_BLOCK_FIELDS = ("kind", "text")

# Optional structural metadata that survives into the clean doc when the
# extractor produced it. These are small dicts, not coordinates — the LLM
# benefits from knowing "this block is a table_cell with row_index=2".
_OPTIONAL_BLOCK_FIELDS = ("table_meta", "table_cell", "figure")


def merge_pages(
    page_jsons: list[dict],
    *,
    rapidocr_pages: dict[int, dict] | None = None,
) -> tuple[dict, dict]:
    """Pure: take per-page JSONs, return (clean_doc, geometry_sidecar).

    Block IDs are sequential across the WHOLE doc in (page_no, reading-order)
    order. So b0001 always precedes b0002, regardless of which page they're
    on. Reading order within a page is approximated by (bbox.y0, bbox.x0).

    When ``rapidocr_pages`` is supplied (page_no -> rapidocr cache dict),
    ``kind="table"`` blocks gain a ``grid`` field recovered from the OCR
    line geometry.
    """
    ordered = sorted(page_jsons, key=lambda p: int(p["page_no"]))
    pages_clean: list[dict] = []
    geometry: dict[str, dict] = {}
    counter = 0

    for p in ordered:
        page_no = int(p["page_no"])
        page_blocks = list(p.get("blocks") or [])

        # Sort blocks within page by reading order. Missing bbox sorts to top
        # — deterministic, but in practice every block has one.
        def _y_then_x(b: dict) -> tuple[float, float]:
            bbox = b.get("bbox") or [0.0, 0.0, 0.0, 0.0]
            return (round(float(bbox[1]), 3), round(float(bbox[0]), 3))
        page_blocks.sort(key=_y_then_x)

        # Table grids. Structured extractors (Azure Content Understanding)
        # ship the grid as table_cell blocks — exact, preferred. The OCR-line
        # geometry recovery stays as the fallback for the RapidOCR path.
        rapid = (rapidocr_pages or {}).get(page_no)
        page_grids: dict[int, dict] = {}        # block index -> grid dict
        for i, b in enumerate(page_blocks):
            if b.get("kind") != "table":
                continue
            g = _grid_from_cells(b, page_blocks)
            if g is None and rapid is not None:
                g = _recover_table_grid(b, rapid)
            if g is not None:
                page_grids[i] = g

        clean_blocks: list[dict] = []
        for i, b in enumerate(page_blocks):
            counter += 1
            block_id = f"b{counter:04d}"

            clean: dict = {"id": block_id}
            for f in _CLEAN_BLOCK_FIELDS:
                if f in b:
                    clean[f] = b[f]
            for f in _OPTIONAL_BLOCK_FIELDS:
                if b.get(f) is not None:
                    clean[f] = b[f]
            if i in page_grids:
                clean["grid"] = page_grids[i]
            # Stash heading bbox-height so _attach_section_paths can
            # bin levels without needing the geometry sidecar. Dropped
            # before output.
            if clean.get("kind") == "heading":
                clean["_h"] = _heading_bbox_height(b)
            clean_blocks.append(clean)

            geo: dict = {"page_no": page_no}
            if b.get("bbox") is not None:
                geo["bbox"] = list(b["bbox"])
            if b.get("polygon") is not None:
                geo["polygon"] = b["polygon"]
            geometry[block_id] = geo

        pages_clean.append({
            "page_no": page_no,
            "page_size": p.get("page_size"),
            "markdown": p.get("markdown", ""),
            "blocks": clean_blocks,
        })

    # Section walk runs across the WHOLE document so heading hierarchy
    # persists across page boundaries.
    _attach_section_paths(pages_clean)

    clean_doc = {"n_pages": len(pages_clean), "pages": pages_clean}
    sidecar = {"blocks": geometry}
    return clean_doc, sidecar


# ---------------------------------------------------------------------------
# Section walk — stamp section_path on every non-heading block.
# ---------------------------------------------------------------------------


# Headings within this fraction of the doc's max heading height are
# treated as a single level (i.e. siblings). Tuned for typical contract
# layouts where h1/h2/h3 differ by ~20-40% in pixel height.
_LEVEL_TOL = 0.15

# Maximum heading depth we track. Beyond this we stay at the deepest level.
_MAX_LEVELS = 3

# A real heading is short. Anything longer than this is almost certainly
# a body paragraph that correct_classify mis-labelled as "heading"; we
# still render its `kind` honestly but skip pushing it onto the section
# stack so the breadcrumb stays clean.
_MAX_HEADING_CHARS = 120


def _heading_bbox_height(block: dict) -> float:
    bbox = block.get("bbox") or [0.0, 0.0, 0.0, 0.0]
    try:
        return float(bbox[3]) - float(bbox[1])
    except (TypeError, ValueError, IndexError):
        return 0.0


def _level_thresholds(heights: list[float]) -> list[float]:
    """Return a sorted-descending list of representative heights, one per
    distinct level. ``len(thresholds) <= _MAX_LEVELS``.

    Strategy: take unique heights sorted descending; greedily cluster ones
    within _LEVEL_TOL of the previous representative into the same level.
    """
    if not heights:
        return []
    uniq = sorted({round(h, 4) for h in heights if h > 0}, reverse=True)
    if not uniq:
        return []

    reps: list[float] = [uniq[0]]
    for h in uniq[1:]:
        if reps[-1] - h <= reps[-1] * _LEVEL_TOL:
            # Same level as previous rep — skip
            continue
        reps.append(h)
        if len(reps) >= _MAX_LEVELS:
            break
    return reps


def _level_of(h: float, thresholds: list[float]) -> int:
    """1-based level. Heights at/above thresholds[0] are level 1,
    at/above thresholds[1] are level 2, etc."""
    if not thresholds:
        return 1
    for i, t in enumerate(thresholds):
        # Same-level tolerance so a heading slightly shorter than its
        # representative still bins with it.
        if h >= t - t * _LEVEL_TOL:
            return i + 1
    return len(thresholds)


def _attach_section_paths(pages: list[dict]) -> None:
    """Mutate ``pages``: attach ``section_path`` to every non-heading
    block based on the running heading stack.

    Two-pass:
      1. Collect heading heights across the doc; compute level thresholds.
      2. Walk blocks in doc order; for headings, update the stack at the
         heading's level (clearing deeper levels). For non-headings, copy
         the current stack labels into ``section_path``.

    Caller stashes each heading block's bbox height onto the clean block
    as ``_h`` (since the clean block doesn't carry bbox). This function
    reads ``_h`` and deletes it before returning.
    """
    # We need heading heights. They were stashed onto blocks during
    # merge_pages (see _stash_heading_height). Collect, then re-walk.
    heights: list[float] = []
    for p in pages:
        for b in p["blocks"]:
            if b.get("kind") == "heading" and "_h" in b:
                heights.append(float(b["_h"]))

    thresholds = _level_thresholds(heights)

    stack: list[str] = [""] * _MAX_LEVELS
    for p in pages:
        for b in p["blocks"]:
            kind = b.get("kind", "")
            if kind == "heading":
                label = (b.get("text") or "").strip()
                # Skip body-paragraph mis-labels — they'd pollute every
                # downstream block's breadcrumb with a 200-char sentence.
                # The block still keeps its `kind: heading` (we don't
                # rewrite correct_classify's call); we just don't anchor
                # the section walk to it.
                if 0 < len(label) <= _MAX_HEADING_CHARS:
                    h = float(b.get("_h", 0.0))
                    lvl = _level_of(h, thresholds)
                    idx = min(lvl, _MAX_LEVELS) - 1
                    stack[idx] = label
                    # Clear deeper levels — the new heading resets descendants
                    for j in range(idx + 1, _MAX_LEVELS):
                        stack[j] = ""
            section_path = [s for s in stack if s]
            if section_path:
                b["section_path"] = section_path
            # Drop the helper field so it never reaches the output
            if "_h" in b:
                del b["_h"]


# ---------------------------------------------------------------------------
# Table grid from structured cells — when the extractor already knows the
# grid (kind="table_cell" blocks sharing the table's table_id), materialise
# it directly. Exact row/column placement, no geometry guessing.
# ---------------------------------------------------------------------------


def _grid_from_cells(table_block: dict, page_blocks: list[dict]) -> dict | None:
    """``{n_rows, n_columns, headers, rows}`` from sibling table_cell blocks,
    or None when the table has no structured cells (OCR-recovery fallback
    applies). Same output shape as ``_recover_table_grid``."""
    meta = table_block.get("table_meta") or {}
    table_id = meta.get("table_id")
    if not table_id:
        return None
    cells = [b for b in page_blocks
             if b.get("kind") == "table_cell"
             and (b.get("table_cell") or {}).get("table_id") == table_id]
    if not cells:
        return None
    n_rows = max(int((c["table_cell"]).get("row_index") or 0) for c in cells) + 1
    n_cols = max(int((c["table_cell"]).get("column_index") or 0) for c in cells) + 1
    grid_rows = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for c in cells:
        tc = c["table_cell"]
        r, k = int(tc.get("row_index") or 0), int(tc.get("column_index") or 0)
        text = str(c.get("text") or "").strip()
        if 0 <= r < n_rows and 0 <= k < n_cols and text:
            cur = grid_rows[r][k]
            grid_rows[r][k] = f"{cur} {text}".strip() if cur else text
    return {
        "n_rows": n_rows,
        "n_columns": n_cols,
        "headers": grid_rows[0],
        "rows": grid_rows[1:],
    }


# ---------------------------------------------------------------------------
# Table grid recovery — cluster OCR lines inside a table block's bbox into
# a (rows × columns) grid. Pure geometry, no LLM.
# ---------------------------------------------------------------------------


# Minimum lines required inside a table block before we attempt grid
# recovery. Smaller tables get the flat-text fallback (no grid attached).
_MIN_LINES_FOR_GRID = 4

# Row tolerance (as a multiple of median line height). Lines whose
# y-midpoints fall within ``median_h * _ROW_TOL`` of the current row anchor
# are merged into the same row.
_ROW_TOL = 0.6

# Column tolerance (fraction of page width). Lines whose x-midpoints fall
# within this distance of an existing column center merge into that column.
_COL_TOL = 0.04


def _recover_table_grid(table_block: dict, rapid: dict) -> dict | None:
    """Return ``{n_rows, n_columns, headers, rows}`` for the given table
    block, or ``None`` when we can't recover a grid.

    ``rapid`` is the RapidOCR cache dict for the same page
    (``{"boxes": [...], "txts": [...], "page_size": [w, h]}``).
    """
    page_size = rapid.get("page_size") or [0, 0]
    try:
        pw, ph = float(page_size[0]), float(page_size[1])
    except (TypeError, ValueError):
        return None
    if pw <= 0 or ph <= 0:
        return None

    bbox = table_block.get("bbox")
    if not bbox or len(bbox) < 4:
        return None
    try:
        bx0, by0, bx1, by1 = (float(bbox[0]), float(bbox[1]),
                              float(bbox[2]), float(bbox[3]))
    except (TypeError, ValueError):
        return None

    # A small inflation to avoid losing lines that sit right on the boundary.
    inflate = 0.005
    bx0 -= inflate; by0 -= inflate
    bx1 += inflate; by1 += inflate

    boxes = rapid.get("boxes") or []
    txts = rapid.get("txts") or []

    lines: list[dict] = []
    for box, txt in zip(boxes, txts):
        text = str(txt or "").strip()
        if not text:
            continue
        try:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
        except (TypeError, ValueError, IndexError):
            continue
        nx0 = min(xs) / pw
        ny0 = min(ys) / ph
        nx1 = max(xs) / pw
        ny1 = max(ys) / ph
        mx = (nx0 + nx1) / 2
        my = (ny0 + ny1) / 2
        if not (bx0 <= mx <= bx1 and by0 <= my <= by1):
            continue
        lines.append({"x0": nx0, "y0": ny0, "x1": nx1, "y1": ny1,
                      "mx": mx, "my": my, "text": text})

    if len(lines) < _MIN_LINES_FOR_GRID:
        return None

    # ---- Row clustering -------------------------------------------------
    lines.sort(key=lambda L: (L["my"], L["mx"]))
    heights = [L["y1"] - L["y0"] for L in lines]
    heights.sort()
    median_h = heights[len(heights) // 2]
    if median_h <= 0:
        return None
    row_tol = median_h * _ROW_TOL

    rows: list[list[dict]] = []
    cur: list[dict] = []
    anchor_y: float | None = None
    for L in lines:
        if anchor_y is None or abs(L["my"] - anchor_y) <= row_tol:
            cur.append(L)
            if anchor_y is None:
                anchor_y = L["my"]
            else:
                # Update anchor as the running mean so rows with slightly
                # drifting baselines still group together.
                anchor_y = (anchor_y * (len(cur) - 1) + L["my"]) / len(cur)
        else:
            cur.sort(key=lambda L: L["mx"])
            rows.append(cur)
            cur = [L]
            anchor_y = L["my"]
    if cur:
        cur.sort(key=lambda L: L["mx"])
        rows.append(cur)

    if len(rows) < 2:
        return None

    # ---- Column clustering ----------------------------------------------
    # Build column centers from line x-midpoints across all rows. Greedy:
    # each new midpoint either snaps to an existing center (within _COL_TOL)
    # or seeds a new one. Then sort centers left-to-right.
    centers: list[float] = []
    for L in lines:
        mx = L["mx"]
        snapped = False
        for i, c in enumerate(centers):
            if abs(c - mx) <= _COL_TOL:
                # Update center as running mean
                centers[i] = (c + mx) / 2
                snapped = True
                break
        if not snapped:
            centers.append(mx)
    centers.sort()

    if not centers:
        return None

    def _col_idx(L: dict) -> int:
        mx = L["mx"]
        best_i, best_d = 0, float("inf")
        for i, c in enumerate(centers):
            d = abs(c - mx)
            if d < best_d:
                best_d, best_i = d, i
        return best_i

    n_cols = len(centers)

    # ---- Materialise grid -----------------------------------------------
    grid_rows: list[list[str]] = []
    for row in rows:
        cells = [""] * n_cols
        for L in row:
            ci = _col_idx(L)
            if cells[ci]:
                cells[ci] = f"{cells[ci]} {L['text']}".strip()
            else:
                cells[ci] = L["text"]
        grid_rows.append(cells)

    if not grid_rows:
        return None

    # First row is treated as the header row. (We don't try to detect
    # multi-row headers yet — that's a follow-up if it shows up in eval.)
    headers = grid_rows[0]
    body = grid_rows[1:] if len(grid_rows) > 1 else []

    return {
        "n_rows": len(grid_rows),
        "n_columns": n_cols,
        "headers": headers,
        "rows": body,
    }


# ---------------------------------------------------------------------------
# Filesystem run
# ---------------------------------------------------------------------------


def _cache_hit(out_path: Path, input_sha: str) -> bool:
    if not out_path.exists():
        return False
    try:
        existing = json.loads(out_path.read_text(encoding="utf-8"))
        return (existing.get("cache") or {}).get("input_sha") == input_sha
    except (json.JSONDecodeError, OSError):
        return False


def _load_rapidocr_pages(rapidocr_dir: Path) -> dict[int, dict]:
    """Read all p_NNN.json under the rapidocr cache dir into a dict keyed
    on page_no. Missing dir / unreadable files are tolerated — we just
    skip the corresponding pages (those tables won't get grids)."""
    out: dict[int, dict] = {}
    if not rapidocr_dir.exists():
        return out
    for f in sorted(rapidocr_dir.glob("p_*.json")):
        try:
            stem = f.stem  # p_NNN
            page_no = int(stem.split("_", 1)[1])
        except (ValueError, IndexError):
            continue
        try:
            out[page_no] = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def run(cfg: Config, doc_id: str, *, force: bool = False) -> MergeResult:
    """Merge per-page JSONs into a clean doc + geometry sidecar."""
    paths = Paths(cfg)
    source_dir = paths.pages_md_dir(doc_id)
    if not source_dir.exists():
        raise FileNotFoundError(
            f"no pages_md/ for doc_id={doc_id} at {source_dir}. "
            "Run pipeline.extraction.correct_classify first."
        )
    page_files = sorted(source_dir.glob("p_*.json"))
    if not page_files:
        raise FileNotFoundError(f"no p_*.json under {source_dir}")

    page_jsons = [json.loads(p.read_text(encoding="utf-8")) for p in page_files]
    rapidocr_pages = _load_rapidocr_pages(paths.rapidocr_dir(doc_id))
    input_sha = _input_sha(page_jsons, rapidocr_pages=rapidocr_pages)

    doc_path = paths.ensure_parent(paths.doc_json(doc_id))
    geom_path = paths.ensure_parent(paths.doc_geometry_json(doc_id))

    # The geometry sidecar is sha-checked too: a kill between the doc and
    # geometry writes must not freeze a stale sidecar behind a fresh doc.
    if not force and _cache_hit(doc_path, input_sha) and _cache_hit(geom_path, input_sha):
        existing = json.loads(doc_path.read_text(encoding="utf-8"))
        n_blocks = sum(len(p.get("blocks") or []) for p in existing.get("pages") or [])
        return MergeResult(
            doc_id=doc_id, n_pages=len(page_jsons), n_blocks=n_blocks,
            cached=True, input_sha=input_sha,
        )

    clean_doc, sidecar = merge_pages(page_jsons, rapidocr_pages=rapidocr_pages)

    clean_payload = {
        "doc_id": doc_id,
        "n_pages": clean_doc["n_pages"],
        "pages": clean_doc["pages"],
        "cache": {"input_sha": input_sha},
    }
    atomic_write_json(doc_path, clean_payload)

    sidecar_payload = {
        "doc_id": doc_id,
        "n_blocks": len(sidecar["blocks"]),
        "blocks": sidecar["blocks"],
        "cache": {"input_sha": input_sha},
    }
    atomic_write_json(geom_path, sidecar_payload)

    n_blocks = sum(len(p.get("blocks") or []) for p in clean_doc["pages"])
    return MergeResult(
        doc_id=doc_id, n_pages=clean_doc["n_pages"], n_blocks=n_blocks,
        cached=False, input_sha=input_sha,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main() -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(
        description="Merge per-page JSONs into clean doc + geometry sidecar.",
    )
    p.add_argument("doc_id")
    p.add_argument("--force", action="store_true",
                   help="Re-merge even if cache hits.")
    args = p.parse_args()

    cfg = Config.load()
    try:
        r = run(cfg, args.doc_id, force=args.force)
    except FileNotFoundError as e:
        sys.stderr.write(f"[FAIL] {e}\n")
        return 2

    print(
        f"[MERGE] doc_id={args.doc_id}  pages={r.n_pages}  blocks={r.n_blocks}  "
        f"{'cached' if r.cached else 'merged'}  input_sha={r.input_sha}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
