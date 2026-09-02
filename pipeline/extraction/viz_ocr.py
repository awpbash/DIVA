"""
viz_ocr.py — overlay OCR detections + adapted blocks on rendered page PNGs.

Visual sanity-check for the extraction pipeline. Reads:

  storage/pages/<doc>/p_NNN.png         the source image
  storage/rapidocr/<doc>/p_NNN.json     raw RapidOCR line detections
  storage/pages_md/<doc>/p_NNN.json     adapted blocks (LLM-corrected + classified)

Writes:

  storage/viz/<doc>/p_NNN.png           annotated image

Overlays:
  - Raw line polygons      thin green outline    (RapidOCR detection)
  - Paragraph bboxes       thick blue rectangle  (adapter output)
  - Heading bboxes         thick red rectangle
  - Table bboxes           thick amber rectangle
  - Table-cell bboxes      thick purple rectangle
  - Figure bboxes          thick brown rectangle

**Coordinate care** (this is the bit that bites you if you're sloppy):

1. PIL images: origin (0,0) at TOP-LEFT, x ascends right, y ascends down.
2. RapidOCR output: SAME convention — pixel coords on the image it was
   called with. Raw `boxes` arrays drop straight onto the image.
3. Adapter output uses NORMALIZED [0,1] coords. To draw those we multiply
   by (W, H) of the actual rendered image — read from PIL, NOT from the
   adapter's ``page_size`` field, defensive against coord-frame drift.

Use ``--max-pages`` to render just the first N pages for spot-checks.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import Config
from ..storage import Paths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_polygons(blob: dict) -> list[list[tuple[float, float]]]:
    """Pixel-coord polygons for each RapidOCR line detection."""
    out: list[list[tuple[float, float]]] = []
    for box in blob.get("boxes") or []:
        try:
            pts = [(float(p[0]), float(p[1])) for p in box]
            if len(pts) == 4:
                out.append(pts)
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _denorm(
    polygon: list[list[float]] | list[float], w: int, h: int,
) -> list[tuple[float, float]] | tuple[float, float, float, float]:
    """Normalized → pixel. Handles both polygon (list of [x,y]) and bbox
    (4 floats x0,y0,x1,y1)."""
    if not polygon:
        return polygon  # type: ignore[return-value]
    if isinstance(polygon[0], (list, tuple)):
        return [(p[0] * w, p[1] * h) for p in polygon]  # type: ignore[index]
    if len(polygon) == 4 and all(isinstance(v, (int, float)) for v in polygon):
        return (polygon[0] * w, polygon[1] * h,
                polygon[2] * w, polygon[3] * h)
    return polygon  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


_RAW_COLOR        = (0, 180, 0, 220)        # green — raw OCR line detections
_PARA_COLOR       = (40, 90, 220, 255)      # blue  — paragraph blocks
_HEADING_COLOR    = (220, 50, 50, 255)      # red   — heading blocks
_TABLE_COLOR      = (240, 150, 0, 255)      # amber — table blocks
_TABLE_CELL_COLOR = (200, 100, 200, 255)    # purple — table_cell blocks
_FIGURE_COLOR     = (160, 100, 60, 255)     # brown — figure blocks


def _try_font(size: int = 14) -> ImageFont.ImageFont | None:
    for name in ("arial.ttf", "DejaVuSans.ttf", "calibri.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_polygon(
    draw: ImageDraw.ImageDraw,
    polygon: list[tuple[float, float]] | list[list[float]],
    *, outline: tuple, width: int = 2,
) -> None:
    """Draw a 4-corner polygon as a closed line outline."""
    pts = [tuple(p) for p in polygon]
    if len(pts) < 3:
        return
    closed = pts + [pts[0]]
    for a, b in zip(closed, closed[1:]):
        draw.line([a, b], fill=outline, width=width)


def _draw_label(
    draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str,
    *, color: tuple, font: ImageFont.ImageFont | None,
) -> None:
    """Small black background + colored text at xy (top-left)."""
    bbox = draw.textbbox(xy, text, font=font)
    pad = 2
    draw.rectangle(
        [bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad],
        fill=(0, 0, 0, 200),
    )
    draw.text(xy, text, fill=color, font=font)


def annotate_page(
    *, png_path: Path,
    raw_blob: dict | None,
    adapted_blob: dict | None,
) -> Image.Image:
    """Open the PNG, draw raw OCR line polygons + adapted block bboxes,
    return the annotated RGBA image. Raw is drawn first so block
    rectangles sit on top — easier to read when raw detections cluster
    densely inside a block."""
    img = Image.open(png_path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _try_font(13)
    W, H = img.size

    # 1. Raw OCR line polygons (pixel coords)
    raw_polys: list[list[tuple[float, float]]] = []
    if raw_blob is not None:
        raw_polys = _raw_polygons(raw_blob)
        for poly in raw_polys:
            _draw_polygon(draw, poly, outline=_RAW_COLOR, width=1)

    # 2. Adapted blocks (normalized → denorm to pixel via image W, H)
    counts = {"para": 0, "head": 0, "tbl": 0, "cell": 0, "fig": 0}
    if adapted_blob is not None:
        for i, block in enumerate(adapted_blob.get("blocks") or []):
            kind = str(block.get("kind") or "")
            bbox_norm = block.get("bbox") or []
            poly_norm = block.get("polygon")
            if len(bbox_norm) != 4:
                continue
            if kind == "heading":
                color = _HEADING_COLOR
                counts["head"] += 1
            elif kind == "table":
                color = _TABLE_COLOR
                counts["tbl"] += 1
            elif kind == "table_cell":
                color = _TABLE_CELL_COLOR
                counts["cell"] += 1
            elif kind == "figure":
                color = _FIGURE_COLOR
                counts["fig"] += 1
            else:
                color = _PARA_COLOR
                counts["para"] += 1

            x0, y0, x1, y1 = _denorm(bbox_norm, W, H)
            draw.rectangle([(x0, y0), (x1, y1)], outline=color, width=3)
            if poly_norm:
                poly_px = _denorm(poly_norm, W, H)
                _draw_polygon(draw, poly_px, outline=color, width=2)
            # Block id label above the box when there's room, else inside.
            block_id = str(block.get("id") or f"#{i}")
            label = f"{block_id} {kind[:4]}"
            label_y = y0 - 16 if y0 >= 16 else y0 + 2
            _draw_label(draw, (x0, label_y), label, color=color, font=font)

    # 3. Caption with counts
    caption = (
        f"raw_lines={len(raw_polys)}  "
        f"para={counts['para']}  head={counts['head']}"
    )
    if counts["tbl"]:  caption += f"  tbl={counts['tbl']}"
    if counts["cell"]: caption += f"  cell={counts['cell']}"
    if counts["fig"]:  caption += f"  fig={counts['fig']}"
    _draw_label(
        draw, (10, 10), caption,
        color=(255, 255, 255, 255), font=font,
    )

    return Image.alpha_composite(img, overlay)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def annotate_doc(
    cfg: Config, doc_id: str, *, max_pages: int | None = None,
) -> dict[str, int]:
    """Annotate every (or first N) rendered page with overlays. Returns
    counts and the output directory."""
    paths = Paths(cfg)
    pages_dir = paths.pages_dir(doc_id)
    if not pages_dir.exists():
        raise FileNotFoundError(
            f"no rendered pages at {pages_dir}. "
            "Run pipeline.extraction.render first."
        )
    pngs = sorted(pages_dir.glob("p_*.png"))
    if not pngs:
        raise FileNotFoundError(f"no PNGs in {pages_dir}")
    if max_pages is not None:
        pngs = pngs[:max_pages]

    out_dir = paths.root / "viz" / doc_id
    out_dir.mkdir(parents=True, exist_ok=True)

    n_done = 0
    n_skipped = 0
    for png_path in pngs:
        try:
            page_no = int(png_path.stem.split("_")[1])
        except (IndexError, ValueError):
            n_skipped += 1
            continue

        raw_blob: dict | None = None
        adapted_blob: dict | None = None

        raw_path = paths.rapidocr_page(doc_id, page_no)
        if raw_path.exists():
            try:
                raw_blob = json.loads(raw_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw_blob = None
        adapted_path = paths.pages_md_page(doc_id, page_no)
        if adapted_path.exists():
            try:
                adapted_blob = json.loads(adapted_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                adapted_blob = None

        if raw_blob is None and adapted_blob is None:
            n_skipped += 1
            continue

        annotated = annotate_page(
            png_path=png_path, raw_blob=raw_blob, adapted_blob=adapted_blob,
        )
        annotated.save(out_dir / png_path.name, format="PNG")
        n_done += 1

    return {"annotated": n_done, "skipped": n_skipped, "out_dir": str(out_dir)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main() -> int:
    import sys
    ap = argparse.ArgumentParser(
        description="Overlay OCR detections + adapted blocks on rendered pages.",
    )
    ap.add_argument("doc_id")
    ap.add_argument(
        "--max-pages", type=int, default=None,
        help="Annotate only the first N pages (spot-check).",
    )
    args = ap.parse_args()

    cfg = Config.load()
    try:
        r = annotate_doc(cfg, args.doc_id, max_pages=args.max_pages)
    except FileNotFoundError as e:
        sys.stderr.write(f"[FAIL] {e}\n")
        return 2
    print(
        f"[VIZ] doc_id={args.doc_id}  annotated={r['annotated']}  "
        f"skipped={r['skipped']}  out={r['out_dir']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
