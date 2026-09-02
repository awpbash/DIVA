"""
table_repair.py — vision repair for badly recovered table grids.

Runs AFTER merge, BEFORE understand. merge.py recovers table grids by
clustering OCR lines — fine for simple tables, garbage for multi-line-cell
layouts (cells interleave mid-sentence; downstream extraction then reads
scrambled text it can never un-scramble).

For each ``kind="table"`` block whose grid fails a quality gate, this stage:

1. crops the table region from the rendered page PNG (bbox from the
   geometry sidecar),
2. sends the crop to the vision model once (cached by crop+prompt sha in
   ``storage/table_repair/<doc_id>/<block_id>.json``),
3. rewrites the block's ``grid`` + flat ``text`` in ``doc/<doc_id>.json``,
4. writes per-row geometry into the sidecar as ``<block_id>:r<N>`` entries
   (row bbox = union of the OCR lines whose text the row contains, with
   the row text stored alongside so validate.py can match snippets to
   rows for row-precise citation bboxes).

Idempotent: re-runs are cache hits unless the page image, the recovered
grid, or the prompt changed.

CLI:
  python -m pipeline.extraction.table_repair <doc_id> [--force]
  python -m pipeline.extraction.table_repair --all
"""
from __future__ import annotations

from . import token_meter

import argparse
import asyncio
import base64
import hashlib
import io
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from ..config import Config
from ..storage import Paths
from .schemas import openai_json_schema

# Repair when more than this fraction of grid cells are empty (the failed
# clusterings produce mostly-empty grids with stray fragments), or when a
# table block has no grid at all.
EMPTY_CELL_GATE = 0.35
# Padding (fraction of page) added around the block bbox before cropping,
# so row edges and borders survive the crop.
CROP_PAD = 0.01
PROMPT_PATH = Path("configs/prompts/table_repair.md")


class TableGrid(BaseModel):
    not_a_table: bool = False
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------


def grid_needs_repair(grid: dict | None) -> bool:
    """True when the heuristic grid is missing or mostly empty."""
    if not grid:
        return True
    headers = list(grid.get("headers") or [])
    rows = list(grid.get("rows") or [])
    cells = list(headers) + [c for r in rows for c in r]
    if not cells:
        return True
    empty = sum(1 for c in cells if not str(c or "").strip())
    return (empty / len(cells)) >= EMPTY_CELL_GATE


# ---------------------------------------------------------------------------
# Crop + LLM
# ---------------------------------------------------------------------------


def _crop_data_url(page_png: Path, bbox: list[float]) -> tuple[str, str]:
    """Crop the normalized bbox (padded) out of the page PNG. Returns
    (data_url, sha16 of the crop bytes)."""
    from PIL import Image

    with Image.open(page_png) as im:
        w, h = im.size
        x0 = max(0, int((bbox[0] - CROP_PAD) * w))
        y0 = max(0, int((bbox[1] - CROP_PAD) * h))
        x1 = min(w, int((bbox[2] + CROP_PAD) * w))
        y1 = min(h, int((bbox[3] + CROP_PAD) * h))
        crop = im.crop((x0, y0, x1, y1))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
    data = buf.getvalue()
    sha = hashlib.sha256(data).hexdigest()[:16]
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{b64}", sha


async def _llm_repair(
    client: AsyncOpenAI, model: str, prompt: str, image_data_url: str,
) -> dict:
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": image_data_url, "detail": "high"}},
                {"type": "text", "text": "Transcribe this table per the schema."},
            ]},
        ],
        response_format={"type": "json_schema",
                         "json_schema": openai_json_schema("table_grid", TableGrid)},
        # Deterministic transcription — repeatable cells across re-runs.
        temperature=0,
        max_completion_tokens=8000,
    )
    token_meter.record(resp.usage, stage="table_repair", model=model)
    return json.loads(resp.choices[0].message.content or "{}")


# ---------------------------------------------------------------------------
# Row geometry — map repaired rows back onto OCR lines
# ---------------------------------------------------------------------------


_WORD_RE = re.compile(r"[a-z0-9$%°.]+")


def _tokens(s: str) -> set[str]:
    return set(_WORD_RE.findall((s or "").lower()))


def _row_bboxes(
    grid: dict, block_bbox: list[float], rapid_page: dict,
) -> dict[int, list[float]]:
    """For each repaired row, union the OCR lines whose tokens that row
    covers. Returns {row_index: [x0,y0,x1,y1] normalized}. Rows with no
    confidently matched line are absent (caller falls back to block bbox).
    """
    pw, ph = rapid_page.get("page_size") or [0, 0]
    if not pw or not ph:
        return {}
    boxes = rapid_page.get("boxes") or []
    txts = rapid_page.get("txts") or []

    # OCR lines inside the (slightly padded) block bbox.
    lines: list[tuple[set[str], list[float]]] = []
    for poly, txt in zip(boxes, txts):
        xs = [p[0] / pw for p in poly]
        ys = [p[1] / ph for p in poly]
        lb = [min(xs), min(ys), max(xs), max(ys)]
        cx, cy = (lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2
        if not (block_bbox[0] - 0.02 <= cx <= block_bbox[2] + 0.02
                and block_bbox[1] - 0.02 <= cy <= block_bbox[3] + 0.02):
            continue
        toks = _tokens(txt)
        if toks:
            lines.append((toks, lb))

    row_tokens = [_tokens(" ".join(r)) for r in (grid.get("rows") or [])]
    out: dict[int, list[float]] = {}
    for toks, lb in lines:
        # Assign each OCR line to the row that covers most of its tokens.
        best_i, best_cov = None, 0.0
        for i, rt in enumerate(row_tokens):
            if not rt:
                continue
            cov = len(toks & rt) / len(toks)
            if cov > best_cov:
                best_i, best_cov = i, cov
        if best_i is None or best_cov < 0.6:
            continue
        cur = out.get(best_i)
        out[best_i] = (lb if cur is None else
                       [min(cur[0], lb[0]), min(cur[1], lb[1]),
                        max(cur[2], lb[2]), max(cur[3], lb[3])])
    return out


def _flatten_grid_text(grid: dict) -> str:
    """Row-major 'header: value' flattening — same shape embed.py uses, so
    block text, embeddings, and snippet grounding all agree."""
    headers = list(grid.get("headers") or [])
    lines: list[str] = []
    for row in grid.get("rows") or []:
        pairs = [f"{(h or 'col').strip()}: {str(c or '').strip()}"
                 for h, c in zip(headers, row) if str(c or "").strip()]
        if pairs:
            lines.append("; ".join(pairs))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage runner
# ---------------------------------------------------------------------------


async def _run_async(cfg: Config, doc_id: str, *, force: bool = False) -> dict:
    paths = Paths(cfg)
    doc_path = paths.doc_json(doc_id)
    geom_path = paths.doc_geometry_json(doc_id)
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    geom = json.loads(geom_path.read_text(encoding="utf-8"))
    blocks_geom = geom.get("blocks") or {}

    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    cache_dir = cfg.storage_root / "table_repair" / doc_id
    cache_dir.mkdir(parents=True, exist_ok=True)

    client = AsyncOpenAI(api_key=cfg.openai_api_key, base_url=cfg.openai_base_url or None)
    stats = {"tables": 0, "repaired": 0, "cached": 0, "llm_calls": 0,
             "not_a_table": 0, "rows_with_bbox": 0}
    changed = False

    for page in doc.get("pages") or []:
        page_no = int(page.get("page_no") or 0)
        rapid_path = cfg.storage_root / "rapidocr" / doc_id / f"p_{page_no:03d}.json"
        rapid = (json.loads(rapid_path.read_text(encoding="utf-8"))
                 if rapid_path.exists() else {})
        for block in page.get("blocks") or []:
            if block.get("kind") != "table":
                continue
            stats["tables"] += 1
            if not force and not grid_needs_repair(block.get("grid")):
                continue
            bid = block["id"]
            entry = blocks_geom.get(bid) or {}
            bbox = entry.get("bbox")
            page_png = paths.pages_dir(doc_id) / f"p_{page_no:03d}.png"
            if not bbox or not page_png.exists():
                continue

            data_url, crop_sha = _crop_data_url(page_png, bbox)
            cache_path = cache_dir / f"{bid}.json"
            cached = None
            if cache_path.exists():
                try:
                    c = json.loads(cache_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    # A torn per-block cache must not crash the repair forever.
                    cache_path.unlink(missing_ok=True)
                    c = {}
                if c.get("crop_sha") == crop_sha and c.get("prompt_sha") == prompt_sha:
                    cached = c.get("grid")
            if cached is not None:
                grid = cached
                stats["cached"] += 1
            else:
                grid = await _llm_repair(client, cfg.vision_model, prompt, data_url)
                stats["llm_calls"] += 1
                cache_path.write_text(json.dumps({
                    "crop_sha": crop_sha, "prompt_sha": prompt_sha, "grid": grid,
                }, ensure_ascii=False, indent=1), encoding="utf-8")

            if grid.get("not_a_table") or not grid.get("rows"):
                stats["not_a_table"] += 1
                continue

            block["grid"] = {"headers": grid.get("headers") or [],
                             "rows": grid.get("rows") or []}
            flat = _flatten_grid_text(block["grid"])
            if flat:
                block["text"] = flat
            stats["repaired"] += 1
            changed = True

            # Per-row geometry sidecar entries: <block_id>:r<N>.
            for i, rb in _row_bboxes(block["grid"], bbox, rapid).items():
                rows = block["grid"]["rows"]
                blocks_geom[f"{bid}:r{i}"] = {
                    "page_no": page_no,
                    "bbox": rb,
                    "row_of": bid,
                    "row_index": i,
                    "row_text": " ".join(c for c in rows[i] if str(c or "").strip()),
                }
                stats["rows_with_bbox"] += 1

    if changed:
        doc_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        geom["blocks"] = blocks_geom
        geom_path.write_text(json.dumps(geom, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    return stats


def run(cfg: Config, doc_id: str, *, force: bool = False) -> dict:
    stats = asyncio.run(_run_async(cfg, doc_id, force=force))
    print(f"[TABLE] {doc_id}: {stats}")
    return stats


def _main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="Vision repair for bad table grids.")
    ap.add_argument("doc_id", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="Repair every table, not just gate failures.")
    args = ap.parse_args()
    cfg = Config.load()
    if args.all:
        doc_ids = sorted(p.stem for p in (cfg.storage_root / "doc").glob("*.json"))
    elif args.doc_id:
        doc_ids = [args.doc_id]
    else:
        ap.error("doc_id or --all")
        return 2
    for d in doc_ids:
        run(cfg, d, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
