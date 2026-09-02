"""
correct_classify.py — Sequential extractor v2.

Pipeline per page:

  1. RapidOCR (already run; we read storage/rapidocr/<doc>/p_NNN.json) →
     line-level geometry (4-corner polygons in PIXEL coords) + verbatim text.
  2. LLM (vision-capable) sees the page image + numbered OCR lines and
     emits:
        - corrections : line_id -> corrected_text
        - missed      : insert_after_line_id -> new text
        - blocks      : list of (kind, line_ids) groups
  3. apply_corrections() materialises the EFFECTIVE line list (originals,
     possibly text-replaced, missed lines spliced in).
  4. assemble_blocks() builds the final VisionBlock list. Each block's
     bbox + polygon are the UNION of its constituent line geometries —
     the LLM never outputs coordinates.

Output goes to storage/pages_md/<doc>/p_NNN.json with the same
VisionPageResponse shape `merge.py` already consumes downstream. Drop-in
replacement for the existing pages_md/<doc>/ output once we've validated
the quality side-by-side.
"""
from __future__ import annotations

from . import token_meter

import argparse
import asyncio
import base64
import difflib
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI
from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import Config
from ..storage import Paths
from .loader import render_prompt
from .schemas import (
    CorrectAndClassifyResponse,
    VisionBlock,
    VisionPageResponse,
    openai_json_schema,
)

log = logging.getLogger(__name__)


LlmCall = Callable[..., Awaitable[dict]]


# ---------------------------------------------------------------------------
# Effective-line model
# ---------------------------------------------------------------------------


@dataclass
class EffectiveLine:
    """One line after corrections + missed-line insertion.

    All geometry is in **normalised [0,1]** coords (page-relative). The
    `polygon` is None for LLM-added missed lines (we synthesise a thin
    bbox between neighbours but don't claim a polygon).

    `raw_text` is the pristine RapidOCR text — the verbatim anchor. It is
    never mutated. `text` may diverge from it only when a correction was
    ACCEPTED (small edit, digits preserved on confident lines). For a
    rejected correction the two stay equal and `source="ocr_flagged"`.
    `raw_text` is "" for `llm_missed` lines (no OCR origin to anchor to).
    """
    text: str
    raw_text: str                      # pristine OCR text (verbatim anchor)
    bbox: list[float]                  # [x0,y0,x1,y1] normalised
    polygon: list[list[float]] | None
    # "ocr" | "ocr_corrected" | "ocr_flagged" | "llm_missed"
    source: str
    original_line_id: int | None       # original RapidOCR index; None for missed
    score: float                       # OCR confidence (0.5 for llm_missed)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _normalize_bbox(
    bbox_px: list[float], page_w: float, page_h: float,
) -> list[float]:
    if page_w <= 0 or page_h <= 0:
        return list(bbox_px)
    return [bbox_px[0] / page_w, bbox_px[1] / page_h,
            bbox_px[2] / page_w, bbox_px[3] / page_h]


def _normalize_polygon(
    polygon_px: list[list[float]], page_w: float, page_h: float,
) -> list[list[float]]:
    if page_w <= 0 or page_h <= 0:
        return polygon_px
    return [[p[0] / page_w, p[1] / page_h] for p in polygon_px]


def _polygon_bbox(polygon: list[list[float]]) -> list[float]:
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def _ocr_lines_to_effective(
    rapid_raw: dict, *,
    min_score: float = 0.30,
) -> list[EffectiveLine]:
    """Convert the raw RapidOCR cache (PIXEL coords) into a list of
    EffectiveLine in NORMALISED coords. Filters low-confidence + empty
    text lines, same convention as the existing rapidocr adapter."""
    page_size = rapid_raw.get("page_size") or [0, 0]
    page_w, page_h = float(page_size[0]), float(page_size[1])
    boxes = rapid_raw.get("boxes") or []
    txts = rapid_raw.get("txts") or []
    scores = rapid_raw.get("scores") or []

    out: list[EffectiveLine] = []
    for i, (box, txt, sc) in enumerate(zip(boxes, txts, scores)):
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
            poly_px = [[float(box[j][0]), float(box[j][1])] for j in range(4)]
        except (TypeError, ValueError, IndexError):
            continue
        poly = _normalize_polygon(poly_px, page_w, page_h)
        bbox = _polygon_bbox(poly)
        out.append(EffectiveLine(
            text=text, raw_text=text, bbox=bbox, polygon=poly,
            source="ocr", original_line_id=i, score=score,
        ))
    return out


def _gap_bbox(
    prev: EffectiveLine | None, nxt: EffectiveLine | None,
) -> list[float]:
    """Synthetic bbox for a missed line, derived from its neighbours.

    Strategy:
      - prev and nxt both present: thin horizontal strip in the vertical
        gap between them, using the union of their x-extents.
      - only nxt (top-of-page missed): strip above nxt's top.
      - only prev (bottom-of-page missed): strip below prev's bottom.
      - neither: full-page-width strip in the middle (page is empty OCR).
    """
    STRIP_H = 0.015                              # thin vertical strip
    if prev is None and nxt is None:
        return [0.05, 0.5 - STRIP_H / 2, 0.95, 0.5 + STRIP_H / 2]
    if prev is None and nxt is not None:
        nx0, ny0, nx1, _ = nxt.bbox
        top = max(0.0, ny0 - STRIP_H)
        return [nx0, top, nx1, ny0]
    if nxt is None and prev is not None:
        px0, _, px1, py1 = prev.bbox
        bot = min(1.0, py1 + STRIP_H)
        return [px0, py1, px1, bot]
    # Both present
    px0, _, px1, py1 = prev.bbox             # type: ignore[union-attr]
    nx0, ny0, nx1, _ = nxt.bbox              # type: ignore[union-attr]
    x0 = min(px0, nx0)
    x1 = max(px1, nx1)
    return [x0, py1, x1, ny0] if ny0 > py1 else [x0, py1, x1, py1 + STRIP_H]


# --- Correction guard (the verbatim anchor) -------------------------------
#
# correct_classify's job is to CLEAN the OCR (de-glue words, restore text the
# OCR dropped) WITHOUT letting the LLM substitute or relocate text onto a
# line's bbox — that would corrupt the geometry anchor a citation highlights.
#
# A correction is REJECTED (text reverts to raw OCR, source="ocr_flagged") when
# EITHER:
#   a) it DISCARDS most of what the OCR actually read — the OCR's characters do
#      not survive (in order) in the proposal. A genuine cleanup KEEPS the OCR
#      content and adds to it (re-spaced, dropped prefix restored), so the raw
#      text is a near-subsequence of the correction; a wholesale substitution /
#      relocation is not. We measure CONTENT PRESERVED, not amount changed.
#   b) it swaps a digit RapidOCR read confidently for a *different* digit
#      (e.g. 'S$500'->'S$600') — a precise hallucination signal.
#
# (History: this used an edit_ratio>0.40 threshold, which rejected legitimate
# LARGE repairs of badly-garbled OCR — e.g. restoring a dropped '"Business Day"'
# term name read as 'arenotrequiredby lawtobeclosed' (edit_ratio 0.54). The
# lines OCR mangles worst need the biggest corrections, so an edit-SIZE rule
# rejects exactly the repairs that matter. Content-preservation fixes that.)
DIGIT_GUARD_MIN_SCORE = 0.90    # protect digits only on confident OCR lines
CONTENT_PRESERVED_MIN = 0.50    # < this fraction of OCR chars surviving → rewrite
CONTENT_CHECK_MIN_LEN = 5       # below this, too few chars to judge relocation
_ASCII_DIGITS = frozenset("0123456789")


def _alnum(s: str) -> str:
    return "".join(c.lower() for c in s if c.isalnum())


def _ocr_content_preserved(raw_text: str, proposed: str) -> float:
    """Fraction of the OCR's alphanumeric characters that survive as
    CONTIGUOUS runs (>= 2 chars) inside the proposed correction.

    Contiguous, not subsequence: a genuine cleanup keeps the OCR's character
    run intact (de-glue / restore dropped text -> ~1.0), while a wholesale
    substitution or relocation leaves only incidental single-char coincidences
    (-> low). A plain subsequence LCS was fooled by SHORT lines — the few
    letters of 'line 1' scatter through 'a completely different sentence' and
    scored 0.8, falsely 'preserved'. Requiring contiguous runs fixes that
    without penalising large faithful repairs (the OCR run stays contiguous in
    them). Spaces, punctuation and case are ignored so re-spacing isn't change."""
    raw_n = _alnum(raw_text)
    prop_n = _alnum(proposed)
    if not raw_n:
        return 1.0   # nothing to anchor to — not a relocation risk
    sm = difflib.SequenceMatcher(None, raw_n, prop_n, autojunk=False)
    preserved = sum(b.size for b in sm.get_matching_blocks() if b.size >= 2)
    return preserved / len(raw_n)


def _near_duplicate(a: str, b: str, *, thresh: float = 0.85) -> bool:
    """True when two lines are ~the same text (ignoring spaces/punct/case).

    Used to refuse a `missed` insertion that merely re-states an adjacent OCR
    line: the LLM occasionally re-reports an existing line as 'missed', which
    would otherwise splice a duplicate paragraph into the page."""
    an, bn = _alnum(a), _alnum(b)
    if not an or not bn:
        return False
    return difflib.SequenceMatcher(None, an, bn, autojunk=False).ratio() >= thresh


def _digit_value_swapped(raw_text: str, proposed: str) -> bool:
    """True for an unambiguous digit-for-digit swap: raw and proposed are the
    same length and differ ONLY at positions that are ASCII digits on both
    sides (e.g. 'S$500'->'S$600'), with at least one such position.

    Every other kind of edit disqualifies the swap, so this never fires on:
      - de-gluing / re-spacing      → lengths differ;
      - a *relocated* space          → the moved space is a non-digit diff;
      - glyph cleanups ('O'->'0')    → the cleaned position differs but isn't
                                        a digit on the raw side.
    Net effect: fires only on a real value swap, not the space/cleanup edits
    that dominate real corrections.
    """
    if len(raw_text) != len(proposed):
        return False
    swapped = False
    for a, b in zip(raw_text, proposed):
        if a == b:
            continue
        if a in _ASCII_DIGITS and b in _ASCII_DIGITS:
            swapped = True          # digit replaced by a different digit
        else:
            return False            # any non-digit diff → not a clean swap
    return swapped


def _correction_rejected(raw_text: str, proposed: str, score: float) -> bool:
    """True when an LLM correction should be REFUSED in favour of raw OCR.

    Logic-based (not edit-size): reject only when the correction discards most
    of the OCR's reading (substitution / relocation) or swaps a confidently-read
    digit. A large-but-faithful repair (de-glue + restore dropped text) is
    ACCEPTED, because the OCR's content is preserved within it."""
    if score >= DIGIT_GUARD_MIN_SCORE and _digit_value_swapped(raw_text, proposed):
        return True
    # The content-preservation check only makes sense for lines with enough
    # text to relocate. Short lines (list markers '(i)'->'(ii)', single digits,
    # 1-2 char cells) have no content to anchor, so the metric is unreliable
    # there — trust the LLM's image read and rely on the digit guard instead.
    raw_n = _alnum(raw_text)
    if (len(raw_n) >= CONTENT_CHECK_MIN_LEN
            and _ocr_content_preserved(raw_text, proposed) < CONTENT_PRESERVED_MIN):
        return True
    return False


def apply_corrections(
    ocr_lines: list[EffectiveLine],
    corrections: list[dict],
    missed: list[dict],
) -> list[EffectiveLine]:
    """Materialise the effective line list.

    Order:
      1. Build a working copy of ocr_lines.
      2. For each correction, guard it against the raw OCR text. ACCEPT a
         plausible cleanup (text replaced, source="ocr_corrected"); REJECT a
         rewrite / confident-digit overwrite (text kept = raw OCR,
         source="ocr_flagged"). `raw_text` is never mutated either way.
      3. Insert missed lines after their `insert_after_line_id` (in input
         order). Each missed line's bbox is derived from its neighbours;
         polygon is None.

    The returned list is the EFFECTIVE list whose indices ``blocks.line_ids``
    reference.
    """
    # Build a quick lookup from original_line_id -> EffectiveLine
    by_id: dict[int, EffectiveLine] = {
        L.original_line_id: L for L in ocr_lines if L.original_line_id is not None
    }

    # 1. Apply corrections (guarded against the verbatim OCR anchor)
    for c in corrections or []:
        lid = c.get("line_id")
        new_text = c.get("corrected_text")
        if lid is None or new_text is None:
            continue
        try:
            lid = int(lid)
        except (TypeError, ValueError):
            continue
        if lid not in by_id:
            continue
        line = by_id[lid]
        proposed = str(new_text).strip()
        if not proposed or proposed == line.raw_text:
            continue  # empty or no-op correction → keep raw OCR untouched
        if _correction_rejected(line.raw_text, proposed, line.score):
            line.source = "ocr_flagged"      # keep line.text == raw OCR
        else:
            line.text = proposed
            line.source = "ocr_corrected"

    # 2. Group missed by insert_after_line_id, preserving input order
    missed_by_anchor: dict[int, list[dict]] = {}
    for m in missed or []:
        anchor = m.get("insert_after_line_id")
        text = m.get("text")
        if anchor is None or text is None:
            continue
        try:
            anchor = int(anchor)
        except (TypeError, ValueError):
            continue
        text = str(text).strip()
        if not text:
            continue
        missed_by_anchor.setdefault(anchor, []).append({"text": text})

    # 3. Walk ocr_lines in order, splicing missed lines in at the right places.
    #    Dedup guard: a missed line that merely re-states an adjacent OCR line
    #    is a false positive (the LLM re-reported an existing line). We KEEP it
    #    in the list to preserve block line_id alignment, but blank its text so
    #    it can't duplicate the paragraph, and tag it 'llm_missed_dup'.
    def _missed_line(
        mtext: str, prev: EffectiveLine | None, nxt: EffectiveLine | None,
    ) -> EffectiveLine:
        dup = ((prev is not None and _near_duplicate(mtext, prev.text))
               or (nxt is not None and _near_duplicate(mtext, nxt.text)))
        return EffectiveLine(
            text="" if dup else mtext,
            raw_text="",                 # LLM-authored: no OCR anchor
            bbox=_gap_bbox(prev, nxt),
            polygon=None,
            source="llm_missed_dup" if dup else "llm_missed",
            original_line_id=None,
            score=0.5,                   # unverifiable against OCR → low conf
        )

    effective: list[EffectiveLine] = []
    # Top-of-page insertions (anchor=-1)
    for m in missed_by_anchor.get(-1, []):
        nxt = ocr_lines[0] if ocr_lines else None
        effective.append(_missed_line(m["text"], None, nxt))
    # Per original line: emit it, then any missed lines anchored to it
    for i, line in enumerate(ocr_lines):
        effective.append(line)
        anchor = line.original_line_id
        if anchor is None:
            continue
        for m in missed_by_anchor.get(anchor, []):
            nxt = ocr_lines[i + 1] if i + 1 < len(ocr_lines) else None
            effective.append(_missed_line(m["text"], line, nxt))
    return effective


# ---------------------------------------------------------------------------
# Block assembly
# ---------------------------------------------------------------------------


def _union_bbox(bboxes: list[list[float]]) -> list[float]:
    if not bboxes:
        return [0.0, 0.0, 0.0, 0.0]
    xs0 = [b[0] for b in bboxes]
    ys0 = [b[1] for b in bboxes]
    xs1 = [b[2] for b in bboxes]
    ys1 = [b[3] for b in bboxes]
    return [min(xs0), min(ys0), max(xs1), max(ys1)]


def _union_polygon(
    polygons: list[list[list[float]]],
) -> list[list[float]]:
    """Axis-aligned envelope polygon over the union of input polygons.
    None polygons (from missed lines) are ignored — the bbox already
    accounts for their geometry via _gap_bbox()."""
    pts = [pt for poly in polygons if poly for pt in poly]
    if not pts:
        return []
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _kind_text_joiner(kind: str) -> str:
    """How to join the constituent lines' texts into the block's `text`."""
    if kind in ("list",):
        return "\n"
    return " "


_PAGE_NUM_RE = re.compile(r"^\d{1,3}$")


def _is_margin_page_number(text: str, bbox: list[float]) -> bool:
    """A bare 1-3 digit block in the top/bottom margin is a page number, not a
    heading — keep it out of the section/heading structure (the LLM tags these
    inconsistently as 'header'/'heading'). Section numbers ('9.4') carry a dot
    so they don't match; mid-page numerics aren't in the margin band."""
    if not _PAGE_NUM_RE.match(text.strip()):
        return False
    return bbox[1] < 0.07 or bbox[3] > 0.93


def assemble_blocks(
    effective: list[EffectiveLine],
    classified: list[dict],
    *,
    fallback_kind: str = "paragraph",
) -> list[VisionBlock]:
    """Build the final VisionBlock list from the LLM's block classifications.

    Behaviour:
      - Each line_id in `classified` must be in [0, len(effective)) — out-
        of-range references are dropped (logged at caller's discretion).
      - Lines not assigned to any block become their own single-line block
        with `kind=fallback_kind`. Better than dropping them.
      - Duplicate assignments (a line in two blocks) keep only the FIRST
        block; subsequent claims are skipped.
    """
    n = len(effective)
    assigned: set[int] = set()
    out: list[VisionBlock] = []
    joiner_re = " "

    for entry in classified or []:
        raw_kind = str(entry.get("kind") or "").strip()
        kind = raw_kind or fallback_kind
        ids = entry.get("line_ids") or []
        valid_ids: list[int] = []
        for x in ids:
            try:
                v = int(x)
            except (TypeError, ValueError):
                continue
            if 0 <= v < n and v not in assigned:
                valid_ids.append(v)
                assigned.add(v)
        if not valid_ids:
            continue
        lines = [effective[i] for i in valid_ids]
        joiner_re = _kind_text_joiner(kind)
        text = joiner_re.join(L.text for L in lines if L.text).strip()
        if not text:
            continue
        bbox = _union_bbox([L.bbox for L in lines])
        polygon = _union_polygon([L.polygon for L in lines if L.polygon])
        if _is_margin_page_number(text, bbox):
            kind = "footer"
        out.append(VisionBlock(
            kind=kind,
            text=text[:4000],
            bbox=bbox,
            polygon=polygon or None,
        ))

    # Orphan lines → one-line paragraph blocks (preserves coverage)
    for i, L in enumerate(effective):
        if i in assigned:
            continue
        if not L.text:
            continue
        bbox = list(L.bbox)
        kind = "footer" if _is_margin_page_number(L.text, bbox) else fallback_kind
        polygon = L.polygon
        out.append(VisionBlock(
            kind=kind,
            text=L.text[:4000],
            bbox=bbox,
            polygon=polygon if polygon else None,
        ))

    # Reading order (top-to-bottom, then left-to-right)
    out.sort(key=lambda b: (round(b.bbox[1], 3), round(b.bbox[0], 3)))
    return out


# ---------------------------------------------------------------------------
# Page-level orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageStats:
    page_no: int
    n_ocr_lines: int
    n_corrections: int            # corrections the LLM proposed
    n_corrections_rejected: int   # of those, refused by the verbatim guard
    n_missed: int
    n_blocks: int
    cache_hit: bool


def _image_data_url(png_path: Path) -> str:
    data = png_path.read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _sha(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode("utf-8"),
    ).hexdigest()[:16]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def _default_llm_call(
    client: AsyncOpenAI, model: str, system_prompt: str,
    image_data_url: str, schema: dict,
) -> dict:
    """Multimodal completion: system prompt (instructions + line list),
    user message carries the image plus a one-liner trigger."""
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": image_data_url, "detail": "high"}},
                {"type": "text", "text": "Return the JSON object per the schema."},
            ]},
        ],
        response_format={"type": "json_schema", "json_schema": schema},
        # Deterministic: at the default temperature the same page yields
        # different corrections run-to-run (recall swings). 0 makes the pass
        # repeatable — a precondition for trusting/caching the output.
        temperature=0,
        # Dense pages can need a lot of output (60+ lines × corrections +
        # blocks). Bump high enough to survive a definitions / TOC page
        # without truncating. Cost-wise this is a ceiling, not a target.
        max_completion_tokens=24000,
    )
    token_meter.record(resp.usage, stage="correct_classify", model=model)
    content = resp.choices[0].message.content or "{}"
    if resp.choices[0].finish_reason == "length":
        raise RuntimeError(
            "correct_classify LLM hit max_completion_tokens — page exceeds "
            "even 24k completion budget; split or simplify the prompt"
        )
    return json.loads(content)


async def process_page(
    *,
    page_no: int,
    png_path: Path,
    rapid_raw: dict,
    model: str,
    llm: LlmCall,
    cache_path: Path | None = None,
    force: bool = False,
) -> tuple[VisionPageResponse, PageStats]:
    """Run the LLM correct+classify pass on one page, assemble the final
    VisionPageResponse.

    Caches the raw LLM response on (image_sha, lines_sha, prompt_sha, model)
    so re-runs without code changes are free."""
    ocr_lines = _ocr_lines_to_effective(rapid_raw)
    n_lines = len(ocr_lines)

    # Build the prompt with the line list embedded. Include the OCR's
    # own confidence (0.0–1.0): the LLM uses this to know which tokens
    # deserve extra scrutiny. Empirically RapidOCR drops below ~0.7
    # exactly on confusable digits (6↔9, 0↔O, 1↔l/I) — surfacing the
    # score lets the LLM focus its correction budget there.
    line_view = [
        {"id": L.original_line_id, "text": L.text, "score": round(L.score, 3)}
        for L in ocr_lines if L.original_line_id is not None
    ]
    prompt = render_prompt(
        "correct_and_classify",
        page_no=page_no, n_lines=n_lines, lines=line_view,
    )

    # Cache key
    image_bytes = png_path.read_bytes()
    expected = {
        "image_sha":  hashlib.sha256(image_bytes).hexdigest()[:16],
        "lines_sha":  _sha(line_view),
        "prompt_sha": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
        "model":      model,
    }

    raw: dict | None = None
    cache_hit = False
    if cache_path is not None and not force and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if all(cached.get("cache", {}).get(k) == v
                   for k, v in expected.items()):
                raw = cached["response"]
                cache_hit = True
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    if raw is None:
        # Empty page (no OCR lines + no LLM ask) → skip the call
        if n_lines == 0:
            raw = {"corrections": [], "missed": [], "blocks": []}
        else:
            schema = openai_json_schema(
                "correct_and_classify", CorrectAndClassifyResponse,
            )
            image_url = (
                f"data:image/png;base64,"
                f"{base64.b64encode(image_bytes).decode('ascii')}"
            )
            raw = await llm(
                model=model, system_prompt=prompt,
                image_data_url=image_url, schema=schema,
            )

    # Validate BEFORE caching. Writing first meant a response that parsed as
    # JSON but failed the schema was cached anyway, and every later run then
    # replayed it and re-derived the same empty fallback. That page lost all
    # correction and block classification permanently, and the old handler said
    # nothing, so an unreported empty page looked exactly like a page the model
    # read correctly and found nothing to fix on.
    try:
        parsed = CorrectAndClassifyResponse.model_validate(raw)
        schema_ok = True
    except ValidationError as exc:
        # Don't crash the doc. Fall back to raw OCR for this page, and say so.
        schema_ok = False
        parsed = CorrectAndClassifyResponse(corrections=[], missed=[], blocks=[])
        log.warning(
            "correct_classify: page %s response failed the schema, using raw "
            "OCR for it and NOT caching, so a retry can still succeed: %s",
            page_no, exc)

    if not cache_hit and schema_ok and cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "response": raw, "cache": expected,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    corrections = [c.model_dump() for c in parsed.corrections]
    missed = [m.model_dump() for m in parsed.missed]
    classified = [b.model_dump() for b in parsed.blocks]

    effective = apply_corrections(ocr_lines, corrections, missed)
    n_rejected = sum(1 for L in effective if L.source == "ocr_flagged")
    blocks = assemble_blocks(effective, classified)

    markdown = "\n\n".join(
        f"# {b.text}" if b.kind == "heading" else b.text for b in blocks
    ).strip()
    page = VisionPageResponse(markdown=markdown, blocks=blocks)
    stats = PageStats(
        page_no=page_no,
        n_ocr_lines=n_lines,
        n_corrections=len(corrections),
        n_corrections_rejected=n_rejected,
        n_missed=len(missed),
        n_blocks=len(blocks),
        cache_hit=cache_hit,
    )
    return page, stats


# ---------------------------------------------------------------------------
# Doc-level orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocResult:
    doc_id: str
    n_pages: int
    n_blocks: int
    n_corrections: int
    n_corrections_rejected: int
    n_missed: int
    n_llm_calls: int
    n_cache_hits: int


async def run_async(
    cfg: Config, doc_id: str, *,
    llm_call: LlmCall | None = None,
    force: bool = False,
    concurrency: int = 4,
    max_pages: int | None = None,
) -> DocResult:
    paths = Paths(cfg)
    pages_dir = paths.pages_dir(doc_id)
    rapidocr_dir = paths.rapidocr_dir(doc_id)
    if not pages_dir.exists():
        raise FileNotFoundError(f"no rendered pages at {pages_dir}")
    if not rapidocr_dir.exists():
        raise FileNotFoundError(
            f"no RapidOCR output at {rapidocr_dir}. "
            "Run pipeline.extraction.rapidocr_ocr first."
        )
    png_files = sorted(pages_dir.glob("p_*.png"))
    if max_pages is not None:
        png_files = png_files[:max_pages]
    if not png_files:
        raise FileNotFoundError(f"no PNGs in {pages_dir}")

    out_dir = paths.pages_md_dir(doc_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = paths.correct_classify_dir(doc_id)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # (the v2 names that used to live here have been renamed to the
    # pages_md / pages_md_dir / pages_md_page primary path now that the
    # RapidOCR + LLM correct+classify pipeline is the default mode.)

    client: AsyncOpenAI | None = None

    async def _llm(*, model, system_prompt, image_data_url, schema):
        nonlocal client
        if llm_call is not None:
            return await llm_call(
                model=model, system_prompt=system_prompt,
                image_data_url=image_data_url, schema=schema,
            )
        if client is None:
            client = AsyncOpenAI(api_key=cfg.openai_api_key, base_url=cfg.openai_base_url or None)
        return await _default_llm_call(
            client, model, system_prompt, image_data_url, schema,
        )

    sem = asyncio.Semaphore(concurrency)

    async def _one(png_path: Path) -> PageStats:
        page_no = int(png_path.stem.split("_")[1])
        rapid_path = paths.rapidocr_page(doc_id, page_no)
        if not rapid_path.exists():
            return PageStats(page_no, 0, 0, 0, 0, 0, False)
        rapid_raw = json.loads(rapid_path.read_text(encoding="utf-8"))
        async with sem:
            page, stats = await process_page(
                page_no=page_no, png_path=png_path, rapid_raw=rapid_raw,
                model=cfg.vision_model, llm=_llm,
                cache_path=paths.correct_classify_page(doc_id, page_no),
                force=force,
            )
        # Write the per-page output (input to merge.py)
        out_path = paths.pages_md_page(doc_id, page_no)
        out_path.write_text(json.dumps({
            "page_no": page_no,
            "page_size": rapid_raw.get("page_size"),
            "extractor": "rapidocr+llm",
            "markdown": page.markdown,
            "blocks": [b.model_dump(exclude_none=True) for b in page.blocks],
            "stats": {
                "n_ocr_lines":   stats.n_ocr_lines,
                "n_corrections": stats.n_corrections,
                "n_corrections_rejected": stats.n_corrections_rejected,
                "n_missed":      stats.n_missed,
            },
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return stats

    results = await asyncio.gather(*(_one(p) for p in png_files))

    return DocResult(
        doc_id=doc_id,
        n_pages=len(results),
        n_blocks=sum(r.n_blocks for r in results),
        n_corrections=sum(r.n_corrections for r in results),
        n_corrections_rejected=sum(r.n_corrections_rejected for r in results),
        n_missed=sum(r.n_missed for r in results),
        n_llm_calls=sum(0 if r.cache_hit else 1 for r in results),
        n_cache_hits=sum(1 for r in results if r.cache_hit),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main() -> int:
    import sys
    ap = argparse.ArgumentParser(
        description="Sequential extractor v2: RapidOCR → LLM correct+classify.",
    )
    ap.add_argument("doc_id")
    ap.add_argument("--force", action="store_true",
                    help="Bypass per-page cache.")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-pages", type=int, default=None,
                    help="Process only the first N pages (spot check).")
    args = ap.parse_args()

    cfg = Config.load()
    try:
        r = asyncio.run(run_async(
            cfg, args.doc_id, force=args.force,
            concurrency=args.concurrency, max_pages=args.max_pages,
        ))
    except FileNotFoundError as e:
        sys.stderr.write(f"[FAIL] {e}\n")
        return 2
    print(
        f"[V2] doc_id={r.doc_id}  pages={r.n_pages}  blocks={r.n_blocks}  "
        f"llm_calls={r.n_llm_calls}  cache_hits={r.n_cache_hits}  "
        f"corrections={r.n_corrections}  rejected={r.n_corrections_rejected}  "
        f"missed={r.n_missed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
