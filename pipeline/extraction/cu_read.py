"""
cu_read.py — Azure Content Understanding reader (the CU-native read path).

Replaces the RapidOCR + LLM correct/classify PAIR when ``READER=cu``: one
``analyzeBinary`` call per document returns markdown plus layout (paragraphs
with semantic roles, tables with cells, figures) with polygon geometry, so
no separate vision-correction pass is needed. The adapter writes the SAME
``pages_md/<doc>/p_NNN.json`` artifact ``correct_classify.py`` produces, so
``merge.py`` and everything after it are untouched. RapidOCR remains the
free offline default; this module only runs when selected.

Caching: the raw CU response is stored once at ``storage/cu_raw/<doc>.json``.
Doc ids are content-addressed, so a document is billed exactly once, ever;
re-runs (and unit tests) adapt from disk for free.

Coordinates: CU encodes element positions as source strings
``D(page,x1,y1,x2,y2,x3,y3,x4,y4)`` in PAGE UNITS (inches for PDF, pixels
for images). Dividing by the CU page width/height yields the same
normalised [0,1] space the RapidOCR adapter derives from pixels, so
evidence highlighting downstream is unchanged.

API: GA ``2025-11-01`` (the 2024/2025 previews retire 2026-07-15).

    POST {endpoint}/contentunderstanding/analyzers/{analyzer}:analyzeBinary
         ?api-version=2025-11-01           body: raw PDF bytes
    -> 202 + Operation-Location
    GET  {operation-location}              poll until Succeeded/Failed
"""
from __future__ import annotations

import argparse
import json
import re
import time

from ..config import Config
from ..storage import Paths
from .schemas import (
    FigureMeta,
    TableCellMeta,
    TableMeta,
    VisionBlock,
    VisionPageResponse,
)

API_VERSION = "2025-11-01"

# CU paragraph semantic role -> VisionBlock kind. Anything unlisted (or
# role-less) is a plain paragraph. Nothing is dropped: page furniture keeps
# its text under header/footer kinds so evidence anchoring stays possible.
_ROLE_TO_KIND = {
    "title": "heading",
    "sectionHeading": "heading",
    "pageHeader": "header",
    "pageFooter": "footer",
    "pageNumber": "footer",
    "footnote": "paragraph",
    "formulaBlock": "paragraph",
}

_TEXT_CAP = 4000          # VisionBlock.text max_length
_SOURCE_RE = re.compile(r"D\(([^)]*)\)")


# ---------------------------------------------------------------------------
# Pure helpers (testable without a live service)
# ---------------------------------------------------------------------------


def parse_source(source: str | None) -> list[tuple[int, list[list[float]]]]:
    """Parse a CU source string into ``[(page_no, polygon), ...]``.

    A source is one or more ``D(page, x1,y1, ..., xN,yN)`` regions (an
    element that continues across pages or columns carries several).
    Returns polygons as point lists in page units; malformed regions are
    skipped rather than raised — a bad polygon must not sink a whole page.
    """
    out: list[tuple[int, list[list[float]]]] = []
    for m in _SOURCE_RE.finditer(source or ""):
        try:
            nums = [float(x) for x in m.group(1).split(",")]
        except ValueError:
            continue
        if len(nums) < 5 or (len(nums) - 1) % 2:
            continue
        page = int(nums[0])
        pts = [[nums[i], nums[i + 1]] for i in range(1, len(nums) - 1, 2)]
        out.append((page, pts))
    return out


def _envelope(pts: list[list[float]]) -> list[float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


def _norm_bbox(bbox: list[float], w: float, h: float) -> list[float]:
    if w <= 0 or h <= 0:
        return [0.0, 0.0, 0.0, 0.0]
    return [min(max(bbox[0] / w, 0.0), 1.0), min(max(bbox[1] / h, 0.0), 1.0),
            min(max(bbox[2] / w, 0.0), 1.0), min(max(bbox[3] / h, 0.0), 1.0)]


def _norm_poly(pts: list[list[float]], w: float, h: float) -> list[list[float]] | None:
    if w <= 0 or h <= 0 or len(pts) != 4:
        return None
    return [[min(max(p[0] / w, 0.0), 1.0), min(max(p[1] / h, 0.0), 1.0)] for p in pts]


def _primary_region(source: str | None) -> tuple[int, list[list[float]]] | None:
    regions = parse_source(source)
    return regions[0] if regions else None


def _center_in(bbox: list[float], boxes: list[list[float]]) -> bool:
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    return any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes)


def _table_markdown(table: dict) -> str:
    """Flat markdown rendering of a CU table (block ``text`` fallback for
    downstream code that only knows flat text)."""
    n_rows = int(table.get("rowCount") or 0)
    n_cols = int(table.get("columnCount") or 0)
    if n_rows < 1 or n_cols < 1:
        return ""
    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for cell in table.get("cells") or []:
        r, c = int(cell.get("rowIndex") or 0), int(cell.get("columnIndex") or 0)
        if 0 <= r < n_rows and 0 <= c < n_cols:
            txt = str(cell.get("content") or "").replace("|", "\\|").replace("\n", " ")
            grid[r][c] = f"{grid[r][c]} {txt}".strip() if grid[r][c] else txt
    lines = ["| " + " | ".join(grid[0]) + " |",
             "|" + "|".join([" --- "] * n_cols) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in grid[1:]]
    return "\n".join(lines)


def _page_markdown(doc_markdown: str, page: dict) -> str:
    """The page's slice of the document markdown (CU page spans are code-
    point offsets by default, which is exactly Python string indexing)."""
    parts = []
    for span in page.get("spans") or []:
        off = int(span.get("offset") or 0)
        ln = int(span.get("length") or 0)
        if 0 <= off <= len(doc_markdown) and ln > 0:
            parts.append(doc_markdown[off:off + ln])
    return "\n".join(parts).strip()


def to_vision_pages(content: dict) -> dict[int, VisionPageResponse]:
    """Adapt one CU DocumentContent into per-page VisionPageResponse.

    Paragraphs become heading/paragraph/header/footer blocks (role map),
    EXCEPT those whose center falls inside a table region on the same page —
    the table carries that text (as a table block + table_cell blocks), and
    emitting both would duplicate it downstream.
    """
    doc_md = str(content.get("markdown") or "")
    pages = {int(p.get("pageNumber") or 0): p for p in content.get("pages") or []}
    dims = {n: (float(p.get("width") or 0), float(p.get("height") or 0))
            for n, p in pages.items()}
    blocks_by_page: dict[int, list[VisionBlock]] = {n: [] for n in pages}
    table_boxes: dict[int, list[list[float]]] = {n: [] for n in pages}

    # -- tables first (their regions mask overlapping paragraphs) ----------
    for ti, table in enumerate(content.get("tables") or []):
        region = _primary_region(table.get("source"))
        if region is None:
            continue
        pno, pts = region
        if pno not in pages:
            continue
        w, h = dims[pno]
        bbox = _norm_bbox(_envelope(pts), w, h)
        table_boxes[pno].append(bbox)
        table_id = f"t{ti}"
        caption = ((table.get("caption") or {}).get("content") or None)
        n_rows = max(1, int(table.get("rowCount") or 1))
        n_cols = max(1, int(table.get("columnCount") or 1))
        blocks_by_page[pno].append(VisionBlock(
            kind="table",
            text=_table_markdown(table)[:_TEXT_CAP],
            bbox=bbox,
            polygon=_norm_poly(pts, w, h),
            table_meta=TableMeta(table_id=table_id, n_rows=n_rows,
                                 n_columns=n_cols,
                                 caption=caption[:400] if caption else None),
        ))
        for cell in table.get("cells") or []:
            cregion = _primary_region(cell.get("source"))
            text = str(cell.get("content") or "").strip()
            if cregion is None or not text:
                continue
            cpno, cpts = cregion
            if cpno not in pages:
                continue
            cw, ch = dims[cpno]
            blocks_by_page[cpno].append(VisionBlock(
                kind="table_cell",
                text=text[:_TEXT_CAP],
                bbox=_norm_bbox(_envelope(cpts), cw, ch),
                polygon=_norm_poly(cpts, cw, ch),
                table_cell=TableCellMeta(
                    table_id=table_id,
                    row_index=int(cell.get("rowIndex") or 0),
                    column_index=int(cell.get("columnIndex") or 0),
                    row_span=max(1, int(cell.get("rowSpan") or 1)),
                    column_span=max(1, int(cell.get("columnSpan") or 1)),
                    cell_role=("data" if (cell.get("kind") or "content") == "content"
                               else str(cell["kind"])),
                ),
            ))

    # -- figures ------------------------------------------------------------
    for fi, fig in enumerate(content.get("figures") or []):
        region = _primary_region(fig.get("source"))
        if region is None:
            continue
        pno, pts = region
        if pno not in pages:
            continue
        w, h = dims[pno]
        caption = ((fig.get("caption") or {}).get("content") or None)
        text = str(fig.get("description") or "") or (caption or "")
        blocks_by_page[pno].append(VisionBlock(
            kind="figure",
            text=text[:_TEXT_CAP],
            bbox=_norm_bbox(_envelope(pts), w, h),
            polygon=_norm_poly(pts, w, h),
            figure=FigureMeta(figure_id=str(fig.get("id") or f"f{fi}")[:32],
                              caption=caption[:600] if caption else None),
        ))

    # -- paragraphs (everything not owned by a table) ------------------------
    for para in content.get("paragraphs") or []:
        text = str(para.get("content") or "").strip()
        region = _primary_region(para.get("source"))
        if not text or region is None:
            continue
        pno, pts = region
        if pno not in pages:
            continue
        w, h = dims[pno]
        bbox = _norm_bbox(_envelope(pts), w, h)
        if _center_in(bbox, table_boxes.get(pno, [])):
            continue
        blocks_by_page[pno].append(VisionBlock(
            kind=_ROLE_TO_KIND.get(str(para.get("role") or ""), "paragraph"),
            text=text[:_TEXT_CAP],
            bbox=bbox,
            polygon=_norm_poly(pts, w, h),
        ))

    out: dict[int, VisionPageResponse] = {}
    for pno, page in pages.items():
        blocks = blocks_by_page.get(pno, [])
        blocks.sort(key=lambda b: (round(b.bbox[1], 3), round(b.bbox[0], 3)))
        out[pno] = VisionPageResponse(
            markdown=_page_markdown(doc_md, page),
            blocks=blocks,
        )
    return out


# ---------------------------------------------------------------------------
# Live service call
# ---------------------------------------------------------------------------


_POLL_INTERVAL_S = 2.0
_POLL_TIMEOUT_S = 900.0


def analyze_pdf_bytes(cfg: Config, data: bytes) -> dict:
    """One analyzeBinary round-trip: submit, poll, return the final
    operation JSON (status Succeeded, with ``result`` and ``usage``)."""
    import httpx

    if not cfg.cu_endpoint or not cfg.cu_key:
        raise RuntimeError(
            "Content Understanding is not configured: set CU_ENDPOINT and "
            "CU_KEY (or use READER=rapidocr for the local reader).")
    url = (f"{cfg.cu_endpoint}/contentunderstanding/analyzers/"
           f"{cfg.cu_analyzer}:analyzeBinary?api-version={API_VERSION}")
    headers = {"Ocp-Apim-Subscription-Key": cfg.cu_key,
               "Content-Type": "application/pdf"}
    with httpx.Client(timeout=120.0) as client:
        r = client.post(url, headers=headers, content=data)
        if r.status_code != 202:
            raise RuntimeError(f"CU analyze failed: {r.status_code} {r.text[:500]}")
        op_url = r.headers.get("Operation-Location")
        if not op_url:
            raise RuntimeError("CU analyze accepted but no Operation-Location header")
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        while True:
            time.sleep(_POLL_INTERVAL_S)
            p = client.get(op_url, headers={"Ocp-Apim-Subscription-Key": cfg.cu_key})
            p.raise_for_status()
            op = p.json()
            status = str(op.get("status") or "").lower()
            if status == "succeeded":
                return op
            if status in ("failed", "canceled"):
                raise RuntimeError(f"CU analysis {status}: "
                                   f"{json.dumps(op.get('error') or {})[:500]}")
            if time.monotonic() > deadline:
                raise RuntimeError("CU analysis timed out after "
                                   f"{_POLL_TIMEOUT_S:.0f}s ({op_url})")


# ---------------------------------------------------------------------------
# Per-doc orchestration (mirror of rapidocr_ocr.process_doc + correct_classify)
# ---------------------------------------------------------------------------


def process_doc(cfg: Config, doc_id: str, *, force: bool = False) -> dict[str, int]:
    """Analyze one document with CU (or reuse the raw cache) and write the
    final per-page blocks to ``pages_md/`` — the same artifact the RapidOCR
    + correct/classify pair produces, input to merge.py."""
    paths = Paths(cfg)
    raw_cache = paths.cu_raw_json(doc_id)
    if not force and raw_cache.exists():
        op = json.loads(raw_cache.read_text(encoding="utf-8"))
        print(f"[cu] raw cache hit: {raw_cache.name}")
    else:
        pdf = paths.raw_pdf(doc_id)
        if not pdf.exists():
            raise FileNotFoundError(f"no {pdf}")
        op = analyze_pdf_bytes(cfg, pdf.read_bytes())
        raw_cache.parent.mkdir(parents=True, exist_ok=True)
        raw_cache.write_text(json.dumps(op, ensure_ascii=False), encoding="utf-8")
        usage = op.get("usage") or {}
        print(f"[cu] analyzed {doc_id}: pages standard="
              f"{usage.get('documentPagesStandard')} basic={usage.get('documentPagesBasic')}")

    contents = ((op.get("result") or {}).get("contents")) or []
    if not contents:
        raise RuntimeError(f"CU result for {doc_id} has no contents")
    content = contents[0]

    vision_pages = to_vision_pages(content)
    out_dir = paths.pages_md_dir(doc_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    dims = {int(p.get("pageNumber") or 0): p for p in content.get("pages") or []}

    n_blocks = 0
    for pno, page in sorted(vision_pages.items()):
        meta = dims.get(pno, {})
        paths.pages_md_page(doc_id, pno).write_text(json.dumps({
            "page_no": pno,
            # CU units (inches for PDF). Blocks are normalised 0-1, so this
            # is informational, same as the pixel page_size on the OCR path.
            "page_size": [meta.get("width"), meta.get("height")],
            "page_unit": content.get("unit"),
            "extractor": "content_understanding",
            "markdown": page.markdown,
            "blocks": [b.model_dump(exclude_none=True) for b in page.blocks],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        n_blocks += len(page.blocks)
    return {"pages": len(vision_pages), "n_blocks": n_blocks}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main() -> int:
    import sys
    ap = argparse.ArgumentParser(
        description="Azure Content Understanding reader (READER=cu path).")
    ap.add_argument("doc_id", help="Doc id with a PDF at storage/raw/<doc>.pdf")
    ap.add_argument("--force", action="store_true",
                    help="Re-call the service even when a raw cache exists "
                         "(BILLS the document again).")
    args = ap.parse_args()
    cfg = Config.load()
    try:
        counts = process_doc(cfg, args.doc_id, force=args.force)
        print(f"[OK] doc_id={args.doc_id}  pages={counts['pages']}  "
              f"blocks={counts['n_blocks']}")
    except (FileNotFoundError, RuntimeError) as e:
        sys.stderr.write(f"[FAIL] {e}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
