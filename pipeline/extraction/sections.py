"""
sections.py — extract Section nodes deterministically from doc.json.

Legal contracts have a strict numbered hierarchy: ``Section 3.``
contains ``3.1``, ``3.1(a)``, etc. The vision pass already tags heading
blocks with ``kind: heading``; we parse those into a Section list with
``(section_num, title, page_start, page_end, bbox_anchor)``.

This runs at LOAD time, not at extraction time, because:
- Section structure is structural metadata that wraps facts; it doesn't
  produce facts itself.
- The same doc.json blocks feed both the canonical facts (via the
  understand pass) AND the Section list (via this module). One source
  of truth.

Pure functions. No store, no LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Heading grammar.
#
# These patterns are a GENRE assumption, not a universal truth: "Section 3.1",
# "Article 5", "Schedule 1A", "Appendix 2" is how Anglo-American legal drafting
# numbers a document. A technical standard, a tender, or a filing in another
# jurisdiction numbers differently, so the grammar is config with these as the
# documented default.
#
# Override by creating configs/sections.yaml:
#
#     patterns:
#       - kind: clause
#         regex: '^\s*Cl\.\s*(\d+(?:\.\d+)*)\s*[\.\:]?\s*(.*)$'
#         ignorecase: true
#
# Each regex MUST capture exactly two groups: the number and the title. Order
# matters and is preserved: most-specific first, so "Section 3.1" is not caught
# by the bare "3.1" rule. Supplying the file replaces the defaults wholesale
# rather than merging, because a different genre usually wants a different set,
# not these plus extras.
# ---------------------------------------------------------------------------

_DEFAULT_SECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # "Section 3.", "Section 3. Title", "Section 3.1 Title"
    ("numbered_section", re.compile(
        r"^\s*Section\s+(\d+(?:\.\d+)*)\s*[\.\:]?\s*(.*)$",
        re.IGNORECASE,
    )),
    # "Article 5 Title"
    ("article", re.compile(
        r"^\s*Article\s+(\d+(?:\.\d+)*)\s*[\.\:]?\s*(.*)$",
        re.IGNORECASE,
    )),
    # "3.1 Supply of services to Customer"  (numbered subsection)
    ("numbered_subsection", re.compile(
        r"^\s*(\d+\.\d+(?:\.\d+)*)\s+(.+)$",
    )),
    # "Schedule 1A", "Schedule 1A Title"
    ("schedule", re.compile(
        r"^\s*Schedule\s+(\d+[A-Z]?)\s*[\.\:]?\s*(.*)$",
        re.IGNORECASE,
    )),
    # "Appendix 2 Title"
    ("appendix", re.compile(
        r"^\s*Appendix\s+(\d+[A-Z]?)\s*[\.\:]?\s*(.*)$",
        re.IGNORECASE,
    )),
]

_SECTIONS_CONFIG = (Path(__file__).resolve().parents[2]
                    / "configs" / "sections.yaml")


@lru_cache(maxsize=1)
def heading_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """The heading grammar in use: configs/sections.yaml if present, else the
    legal-drafting default above."""
    if not _SECTIONS_CONFIG.exists():
        return tuple(_DEFAULT_SECTION_PATTERNS)
    doc = yaml.safe_load(_SECTIONS_CONFIG.read_text(encoding="utf-8")) or {}
    out: list[tuple[str, re.Pattern[str]]] = []
    for i, entry in enumerate(doc.get("patterns") or []):
        entry = entry or {}
        kind, regex = entry.get("kind"), entry.get("regex")
        if not kind or not regex:
            raise ValueError(
                f"configs/sections.yaml patterns[{i}]: both `kind` and `regex` are required")
        flags = re.IGNORECASE if entry.get("ignorecase") else 0
        compiled = re.compile(regex, flags)
        if compiled.groups != 2:
            raise ValueError(
                f"configs/sections.yaml patterns[{i}] ({kind}): regex must capture "
                f"exactly 2 groups (number, title), got {compiled.groups}")
        out.append((str(kind), compiled))
    if not out:
        raise ValueError("configs/sections.yaml declares no patterns")
    return tuple(out)


# Top-level numbered_section depth is 1 (`Section 3`, depth=1; `Section 3.1`,
# depth=2; `3.1.4`, depth=3). Used by callers to filter top-level only.
def _depth(section_num: str) -> int:
    return len([p for p in section_num.split(".") if p])


@dataclass(frozen=True)
class Section:
    """Logical section in the document.

    ``bbox_anchor`` is the heading block's bbox — used at LOAD time to
    anchor the section node and to determine which EvidenceSpans fall
    'inside' this section (anything on the same page at or below this
    bbox.y0, until the next heading).
    """
    section_id: str            # "<doc_id>:<section_num>"
    doc_id: str
    section_num: str
    title: str
    kind: str                  # one of heading_patterns() keys
    page_start: int
    page_end: int              # filled in after all sections parsed
    bbox_anchor: tuple[float, float, float, float]
    order: int


def _match_heading(text: str) -> tuple[str, str, str] | None:
    """Return (kind, section_num, title) or None."""
    if not text:
        return None
    snippet = text.strip()
    for kind, pat in heading_patterns():
        m = pat.match(snippet)
        if not m:
            continue
        section_num = m.group(1)
        title = (m.group(2) or "").strip().rstrip(".:")
        return kind, section_num, title
    return None


def extract_sections(doc: dict, *, doc_id: str,
                     geometry: dict | None = None) -> list[Section]:
    """Parse a list of Section objects from the doc.

    Algorithm:

    1. Walk pages in order, then blocks in reading order within a page.
    2. For each block with ``kind == "heading"``, try the heading regex.
       On match, emit a Section with the heading's page + bbox.
    3. After the full sweep, set each section's ``page_end`` to the page
       just before the next section starts; the last section runs to the
       final page.

    A heading whose text doesn't match any pattern is ignored (it might
    be a column header or table title rather than a structural heading).
    """
    sections: list[Section] = []
    order = 0
    # doc.pages[].blocks[] don't carry bbox — the per-block geometry
    # lives in the geometry sidecar keyed by block id. When caller
    # passes geometry, look up the heading's bbox from there; without
    # it the section's `bbox_anchor` collapses to a degenerate
    # `[0,0,1,0]` and the frontend can't draw a heading highlight.
    geo_by_id = (geometry or {}).get("blocks") or {}
    for page in doc.get("pages") or []:
        page_no = int(page.get("page_no", 0))
        for block in page.get("blocks") or []:
            if block.get("kind") != "heading":
                continue
            text = str(block.get("text") or "")
            parsed = _match_heading(text)
            if parsed is None:
                continue
            kind, section_num, title = parsed
            bbox = (
                block.get("bbox")
                or (geo_by_id.get(block.get("id")) or {}).get("bbox")
                or [0.0, 0.0, 1.0, 0.0]
            )
            try:
                bbox_t = (float(bbox[0]), float(bbox[1]),
                          float(bbox[2]), float(bbox[3]))
            except (IndexError, TypeError, ValueError):
                continue
            order += 1
            sections.append(Section(
                section_id=f"{doc_id}:{section_num}",
                doc_id=doc_id,
                section_num=section_num,
                title=title,
                kind=kind,
                page_start=page_no,
                page_end=page_no,    # placeholder
                bbox_anchor=bbox_t,
                order=order,
            ))

    if not sections:
        return sections

    total_pages = int(doc.get("n_pages") or
                      max((p.get("page_no", 0) for p in doc.get("pages") or []),
                          default=0))

    # Fill page_end: each section ends just before the next section starts.
    out: list[Section] = []
    for i, sec in enumerate(sections):
        if i + 1 < len(sections):
            next_page = sections[i + 1].page_start
            page_end = max(sec.page_start, next_page - 1 if next_page > sec.page_start
                           else sec.page_start)
        else:
            page_end = total_pages or sec.page_start
        out.append(Section(
            section_id=sec.section_id,
            doc_id=sec.doc_id,
            section_num=sec.section_num,
            title=sec.title,
            kind=sec.kind,
            page_start=sec.page_start,
            page_end=page_end,
            bbox_anchor=sec.bbox_anchor,
            order=sec.order,
        ))
    return out


# ---------------------------------------------------------------------------
# Section assignment for evidence spans
# ---------------------------------------------------------------------------


def assign_section(
    sections: list[Section],
    page_no: int,
    bbox: tuple[float, float, float, float] | list[float] | None,
) -> Section | None:
    """Pick the section a given (page, bbox) falls under.

    A span is "in" the deepest section whose heading PRECEDES it in
    reading order on the same page (or any earlier page). When the span
    has no bbox, fall back to page-only containment (page within
    [page_start, page_end]).

    Returns None if no section precedes — i.e., the span is in the
    document preamble before any numbered heading.
    """
    if not sections:
        return None

    candidates: list[Section] = []
    if bbox is not None and len(bbox) >= 4:
        try:
            span_y0 = float(bbox[1])
        except (TypeError, ValueError):
            span_y0 = None
    else:
        span_y0 = None

    for sec in sections:
        # Section starts before this page entirely.
        if sec.page_start < page_no <= sec.page_end:
            candidates.append(sec)
        elif sec.page_start == page_no:
            # Same page — heading must be at or above the span's top edge.
            if span_y0 is None or sec.bbox_anchor[1] <= span_y0:
                candidates.append(sec)

    if not candidates:
        return None
    # Prefer the deepest (most specific) section number, then latest order.
    candidates.sort(key=lambda s: (_depth(s.section_num), s.order), reverse=True)
    return candidates[0]
