"""
rapidocr_ocr.py — RapidOCR extractor (extract branch).

Pure-pip alternative to Tesseract. No system install: rapidocr ships
ONNX models that download lazily on first run (~30MB). Line-level
detection by default; we group lines into paragraphs via a vertical-
proximity heuristic so the downstream pipeline sees paragraph-grain
blocks (matching the LLM-vision and Tesseract adapters' contract).

**Stub-runnable**: the adapter (``to_vision_page``, paragraph grouping)
is pure Python and tests against captured fixture dicts without invoking
the OCR engine.

RapidOCR output shape (v3.x, via rapidocr.RapidOCR()):

    output.boxes   ndarray shape (N, 4, 2)  -- 4 corners per line
    output.txts    tuple[str] length N
    output.scores  tuple[float] length N    -- confidence 0..1

The 4 corners are TL, TR, BR, BL in image-pixel coordinates. No skew
metadata — we treat them as a real quadrilateral (preserve in polygon)
plus axis-aligned envelope (bbox).

**Cache layout**:

    storage/rapidocr/<doc>/p_NNN.json            raw line detections
    storage/pages_md_rapidocr/<doc>/p_NNN.json   adapted VisionPageResponse
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..config import Config
from ..storage import Paths
from .schemas import VisionBlock, VisionPageResponse


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Lines below this confidence get dropped from extracted text (matches the
# spirit of Tesseract's WORD_CONF_THRESHOLD=30 from the other adapter).
LINE_SCORE_THRESHOLD = 0.30


# Paragraph grouping: two consecutive lines belong to the same paragraph
# when their vertical gap is < this multiple of their average line height.
PARAGRAPH_GAP_RATIO = 1.2


# Heading detection: paragraph average line-height >= MEDIAN * ratio AND
# text shorter than CAP.
HEADING_HEIGHT_RATIO = 1.4
HEADING_TEXT_LEN_CAP = 120


# ---------------------------------------------------------------------------
# Pure helpers (testable without RapidOCR installed)
# ---------------------------------------------------------------------------


def _polygon_bbox(polygon: list[list[float]]) -> list[float]:
    """Axis-aligned envelope of a 4-corner polygon."""
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def _normalize_polygon(
    polygon: list[list[float]], page_w: float, page_h: float,
) -> list[list[float]]:
    if page_w <= 0 or page_h <= 0:
        return polygon
    return [[p[0] / page_w, p[1] / page_h] for p in polygon]


def _normalize_bbox(
    bbox: list[float], page_w: float, page_h: float,
) -> list[float]:
    if page_w <= 0 or page_h <= 0:
        return list(bbox)
    return [bbox[0] / page_w, bbox[1] / page_h,
            bbox[2] / page_w, bbox[3] / page_h]


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _filter_and_dictify(
    boxes: list[list[list[float]]], txts: list[str], scores: list[float],
    *, min_score: float = LINE_SCORE_THRESHOLD,
) -> list[dict]:
    """Confidence-filter + drop empty text + zip into ``{polygon, text, score,
    bbox, height}`` dicts in PIXEL coords. Output preserves the input order."""
    out: list[dict] = []
    for box, txt, sc in zip(boxes, txts, scores):
        try:
            score = float(sc)
        except (TypeError, ValueError):
            continue
        if score < min_score:
            continue
        text = str(txt or "").strip()
        if not text:
            continue
        try:
            polygon = [[float(box[i][0]), float(box[i][1])] for i in range(4)]
        except (TypeError, ValueError, IndexError):
            continue
        bbox = _polygon_bbox(polygon)
        out.append({
            "polygon": polygon,
            "bbox": bbox,
            "text": text,
            "score": score,
            "height": bbox[3] - bbox[1],
        })
    return out


def _group_lines_into_paragraphs(
    lines: list[dict], *, gap_ratio: float = PARAGRAPH_GAP_RATIO,
) -> list[list[dict]]:
    """Group sorted lines into paragraphs by vertical proximity.

    Two consecutive lines join the same paragraph when the gap between
    them (line[i].bbox.y_top - line[i-1].bbox.y_bottom) is less than
    ``gap_ratio`` × the average line height. Beyond that, a new paragraph
    starts. Single-column reading flow assumption — multi-column pages
    will produce one paragraph per column (acceptable for the PoC).
    """
    if not lines:
        return []
    # Sort by y-top, then x-left
    sorted_lines = sorted(lines, key=lambda L: (L["bbox"][1], L["bbox"][0]))
    paragraphs: list[list[dict]] = [[sorted_lines[0]]]
    for line in sorted_lines[1:]:
        prev = paragraphs[-1][-1]
        gap = line["bbox"][1] - prev["bbox"][3]   # top of this - bottom of prev
        avg_h = (line["height"] + prev["height"]) / 2.0
        if avg_h <= 0:
            paragraphs.append([line])
            continue
        if gap < gap_ratio * avg_h:
            paragraphs[-1].append(line)
        else:
            paragraphs.append([line])
    return paragraphs


def _paragraph_envelope_polygon(lines: list[dict]) -> list[list[float]]:
    """Tight rectangular envelope (4 corners) of the union of all line
    polygons in this paragraph. Axis-aligned — RapidOCR doesn't give us
    skew info."""
    xs = [pt[0] for L in lines for pt in L["polygon"]]
    ys = [pt[1] for L in lines for pt in L["polygon"]]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def to_vision_page(
    raw: dict, *, page_size: tuple[float, float],
) -> VisionPageResponse:
    """RapidOCR raw output (boxes/txts/scores arrays) -> VisionPageResponse.

    Steps:
      1. Confidence + empty-text filter (LINE_SCORE_THRESHOLD)
      2. Group lines into paragraphs (vertical proximity)
      3. For each paragraph: emit one VisionBlock with text = lines joined
         by " ", bbox = envelope, polygon = envelope (axis-aligned)
      4. Heading heuristic: average line height >= median × ratio AND
         text len <= cap -> kind="heading", else "paragraph"
    """
    page_w, page_h = page_size
    boxes = raw.get("boxes") or []
    txts = raw.get("txts") or []
    scores = raw.get("scores") or []

    lines = _filter_and_dictify(boxes, txts, scores)
    if not lines:
        return VisionPageResponse(markdown="", blocks=[])

    paragraphs = _group_lines_into_paragraphs(lines)

    # Page-level median line height for heading heuristic
    median_h = _median([L["height"] for L in lines if L["height"] > 0])
    heading_threshold = median_h * HEADING_HEIGHT_RATIO if median_h > 0 else 0.0

    blocks: list[VisionBlock] = []
    md_parts: list[str] = []
    for para_lines in paragraphs:
        text = " ".join(L["text"] for L in para_lines)
        if not text.strip():
            continue
        env_poly_px = _paragraph_envelope_polygon(para_lines)
        env_bbox_px = _polygon_bbox(env_poly_px)
        bbox = _normalize_bbox(env_bbox_px, page_w, page_h)
        polygon = _normalize_polygon(env_poly_px, page_w, page_h)

        avg_h = sum(L["height"] for L in para_lines) / len(para_lines)
        is_heading = (
            heading_threshold > 0
            and avg_h >= heading_threshold
            and len(text) <= HEADING_TEXT_LEN_CAP
        )
        kind = "heading" if is_heading else "paragraph"

        blocks.append(VisionBlock(
            kind=kind,
            text=text[:4000],
            bbox=bbox,
            polygon=polygon,
        ))
        md_parts.append(f"# {text}" if is_heading else text)

    # Reading order: top-to-bottom, then left-to-right
    blocks.sort(key=lambda b: (round(b.bbox[1], 3), round(b.bbox[0], 3)))
    return VisionPageResponse(
        markdown="\n\n".join(md_parts).strip(),
        blocks=blocks,
    )


# ---------------------------------------------------------------------------
# Live extraction
# ---------------------------------------------------------------------------


def _check_rapidocr_available() -> tuple[bool, str]:
    try:
        # Import for the side effect of failing when OCR is not installed.
        import rapidocr           # type: ignore  # noqa: F401
        from rapidocr import RapidOCR  # type: ignore  # noqa: F401
        return True, "rapidocr available"
    except ImportError:
        return False, ("rapidocr not installed. "
                       "pip install rapidocr  (>=3.0 supports Python 3.13+).")


# Lazy singleton — model load is ~1s, reuse across pages.
_ENGINE_SINGLETON = None


def _get_engine():
    """RapidOCR configured with PP-OCRv5 (mobile) det+rec+cls.

    The library default is PP-OCRv4 ch-mobile, whose RECOGNIZER emits garbage
    on some English contract lines — scoring them below RapidOCR's 0.5
    text_score floor so they're silently dropped (e.g. the "Business Day"
    definition head line). PP-OCRv5 reads those cleanly, and v5-mobile matches
    v5-server quality on these 300-DPI scans at a fraction of the cost. The
    win is recognition, not detection — so the cheaper mobile models suffice.
    """
    global _ENGINE_SINGLETON
    if _ENGINE_SINGLETON is None:
        from rapidocr import RapidOCR                              # type: ignore
        from rapidocr.utils.typings import ModelType, OCRVersion   # type: ignore
        v5, mobile = OCRVersion("PP-OCRv5"), ModelType("mobile")
        _ENGINE_SINGLETON = RapidOCR(params={
            "Det.ocr_version": v5, "Det.model_type": mobile,
            "Rec.ocr_version": v5, "Rec.model_type": mobile,
            "Cls.ocr_version": v5,
        })
    return _ENGINE_SINGLETON


def extract_page(png_path: Path) -> dict:
    """Run RapidOCR on one rendered page PNG. Returns:

        {"boxes": [[[x,y],...]*N], "txts": [str]*N, "scores": [float]*N,
         "page_size": [w, h]}
    """
    ok, msg = _check_rapidocr_available()
    if not ok:
        raise RuntimeError(msg)
    from PIL import Image
    engine = _get_engine()
    output = engine(str(png_path))
    with Image.open(png_path) as img:
        page_size = list(img.size)
    boxes = []
    if output.boxes is not None and len(output.boxes) > 0:
        for row in output.boxes:
            boxes.append([[float(x), float(y)] for x, y in row])
    return {
        "boxes":     boxes,
        "txts":      [str(t) for t in (output.txts or [])],
        "scores":    [float(s) for s in (output.scores or [])],
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Per-doc orchestration
# ---------------------------------------------------------------------------


def process_doc(
    cfg: Config, doc_id: str, *, force: bool = False,
) -> dict[str, int]:
    """Run RapidOCR on every rendered page of a doc. Writes raw + adapted
    outputs side-by-side with the LLM-vision + Tesseract sibling dirs."""
    paths = Paths(cfg)
    pages_dir = paths.pages_dir(doc_id)
    if not pages_dir.exists():
        raise FileNotFoundError(
            f"no rendered pages at {pages_dir}. "
            "Run pipeline.extraction.render first."
        )
    png_files = sorted(pages_dir.glob("p_*.png"))
    if not png_files:
        raise FileNotFoundError(f"no PNGs in {pages_dir}")

    raw_dir = paths.rapidocr_dir(doc_id)
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_dir = paths.pages_md_rapidocr_dir(doc_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    n_pages = 0
    n_blocks = 0
    for png_path in png_files:
        try:
            page_no = int(png_path.stem.split("_")[1])
        except (IndexError, ValueError):
            continue
        raw_cache = paths.rapidocr_page(doc_id, page_no)
        if not force and raw_cache.exists():
            blob = json.loads(raw_cache.read_text(encoding="utf-8"))
        else:
            blob = extract_page(png_path)
            raw_cache.write_text(
                json.dumps(blob, ensure_ascii=False), encoding="utf-8",
            )
        page_size = blob.get("page_size") or [0, 0]
        page = to_vision_page(
            blob, page_size=tuple(map(float, page_size)),
        )
        page_path = paths.pages_md_rapidocr_page(doc_id, page_no)
        page_path.write_text(json.dumps({
            "page_no": page_no,
            "page_size": {"w": page_size[0], "h": page_size[1]},
            "extractor": "rapidocr",
            "markdown": page.markdown,
            "blocks": [b.model_dump(exclude_none=True) for b in page.blocks],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        n_pages += 1
        n_blocks += len(page.blocks)
    return {"pages": n_pages, "n_blocks": n_blocks}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main() -> int:
    import sys
    ap = argparse.ArgumentParser(
        description="RapidOCR extractor (extract branch).",
    )
    ap.add_argument("doc_id", help="Doc id with rendered pages in "
                                   "storage/pages/<doc>/p_NNN.png.")
    ap.add_argument("--force", action="store_true",
                    help="Re-run RapidOCR even when raw cache exists.")
    ap.add_argument("--check", action="store_true",
                    help="Just verify rapidocr is installed + importable.")
    args = ap.parse_args()

    if args.check:
        ok, msg = _check_rapidocr_available()
        sys.stdout.write(("[OK] " if ok else "[FAIL] ") + msg + "\n")
        return 0 if ok else 2

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
