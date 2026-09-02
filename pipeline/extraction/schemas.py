"""
Pydantic models for LLM structured-output responses (v2).

Each model serves two roles:

1. **Schema generator** — ``model.model_json_schema()`` produces the JSON
   Schema we hand to OpenAI's ``response_format={"type": "json_schema", ...}``.
2. **Parser/validator** — ``model.model_validate(raw)`` turns the LLM's
   reply into a typed object, with locations on every validation error.

v2 covers the three understand-pass responses (outline, harvest, categorise)
plus a vision page response. The legacy per-field response models from v1
are gone.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    """Common base: forbid extras."""
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Vision (step 2 — one call per page)
# ---------------------------------------------------------------------------


class TableCellMeta(_Strict):
    """Per-cell metadata attached to a ``kind="table_cell"`` VisionBlock.

    Lifted from Azure Content Understanding's table.cells output (or any
    equivalent CV-based layout extractor). Lets downstream reason about a
    table as a grid rather than as one giant rectangle.
    """
    table_id: str = Field(max_length=32,
                          description="ID of the parent table; same value "
                                      "appears on the table-level block "
                                      "(kind='table') in this page's block list.")
    row_index: int = Field(ge=0)
    column_index: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    column_span: int = Field(default=1, ge=1)
    cell_role: str = Field(default="data",
                           description="One of: data, columnHeader, rowHeader, "
                                       "stubHead, description, footer")


class TableMeta(_Strict):
    """Per-table metadata attached to a ``kind="table"`` VisionBlock.

    The block's ``text`` field carries a markdown rendering of the table for
    backwards compatibility with downstream code that only knows about
    flat-text blocks. Structured access goes through ``TableMeta`` + the
    sibling ``kind="table_cell"`` blocks that share this table's
    ``table_id``.
    """
    table_id: str = Field(max_length=32)
    n_rows: int = Field(ge=1)
    n_columns: int = Field(ge=1)
    caption: Optional[str] = Field(default=None, max_length=400)


class FigureMeta(_Strict):
    """Per-figure metadata for ``kind="figure"`` blocks.

    Carries the figure's caption (text was previously stuffed into the
    block ``text`` field; now separated so we can keep the picture's
    region distinct from its caption's region)."""
    figure_id: Optional[str] = Field(default=None, max_length=32)
    caption: Optional[str] = Field(default=None, max_length=600)


class VisionBlock(_Strict):
    """One structural block on a page. NO semantic interpretation.

    Backwards compatible with v2:
    - Existing producers (LLM vision_layout.md) populate only kind/text/bbox.
    - New producers (Azure Content Understanding) can additionally populate
      ``polygon`` (4 corner points; preserves rotation/skew) and the
      kind-specific meta dicts (``table_meta``, ``table_cell``, ``figure``).
    """
    kind: str = Field(description="One of: heading, paragraph, list, list_item, "
                                  "table, table_cell, figure, caption, footer, header, "
                                  "key_value_pair")
    text: str = Field(max_length=4000,
                      description="Verbatim text content (up to 4000 chars to "
                                  "fit concatenated table cells; the LLM-vision "
                                  "path caps itself at ~2000).")
    bbox: list[float] = Field(min_length=4, max_length=4,
                              description="[x0, y0, x1, y1] axis-aligned envelope, "
                                          "normalised 0-1.")
    polygon: Optional[list[list[float]]] = Field(
        default=None,
        description="Optional 4-corner quadrilateral, [[x0,y0],[x1,y1],[x2,y2],[x3,y3]] "
                    "normalised 0-1. Preserves rotation/skew when the source is a CV-"
                    "based detector (Azure CU, Tesseract). LLM vision path leaves this "
                    "null.",
    )
    table_meta: Optional[TableMeta] = None
    table_cell: Optional[TableCellMeta] = None
    figure: Optional[FigureMeta] = None


class VisionPageResponse(_Strict):
    """Response for one page from the vision LLM (or the Azure CU adapter)."""
    markdown: str
    blocks: list[VisionBlock] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Correct + classify (extract branch v2 — OCR-first, LLM refines + groups)
# ---------------------------------------------------------------------------
#
# This is the sequential pipeline: RapidOCR produces per-line geometry +
# verbatim text first; the LLM then sees the image plus those lines and
# emits THREE things:
#   1. corrections to lines the OCR misread
#   2. missed lines the OCR didn't pick up (positioned by "after line N")
#   3. blocks (groups of line_ids) with a kind label
#
# The LLM never outputs coordinates. Block geometry is always derived
# from the union of the underlying RapidOCR line polygons.


class CorrectedLine(_Strict):
    """Replace the OCR text on this line. Used when the OCR misread a word
    or two but got the line position right."""
    line_id: int = Field(
        ge=0,
        description="0-based index into the RapidOCR line list for this page.",
    )
    corrected_text: str = Field(max_length=4000)


class MissedLine(_Strict):
    """Insert a new line the OCR didn't detect. Position is given by the
    line it should follow; bbox is derived as the gap between that line
    and the next (or below the last)."""
    insert_after_line_id: int = Field(
        ge=-1,
        description="0-based index of the line this missed text should "
                    "follow. Use -1 to insert at the top of the page.",
    )
    text: str = Field(max_length=4000)


class ClassifiedBlockV2(_Strict):
    """One block, given as the list of line_ids (after corrections +
    missed-text application) that compose it, plus a kind label."""
    kind: str = Field(
        description="One of: heading, paragraph, list, list_item, table, "
                    "table_cell, figure, caption, footer, header, "
                    "key_value_pair",
    )
    line_ids: list[int] = Field(
        min_length=1,
        description="0-based indices into the EFFECTIVE line list (after "
                    "corrections + missed-line insertion, in reading order). "
                    "Each line_id may only appear in ONE block.",
    )


class CorrectAndClassifyResponse(_Strict):
    """One per-page response from the LLM correct+classify pass."""
    corrections: list[CorrectedLine] = Field(default_factory=list)
    missed: list[MissedLine] = Field(default_factory=list)
    blocks: list[ClassifiedBlockV2] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Outline (step 4a)
# ---------------------------------------------------------------------------


class OutlineSection(_Strict):
    label: str = Field(max_length=120)
    page_range: list[int] = Field(min_length=2, max_length=2,
                                  description="[start, end] inclusive")


class OutlineResponse(_Strict):
    """Step 4a output: doctype + structural outline."""
    doctype: str = Field(max_length=64)
    doctype_confidence: float = Field(ge=0, le=1)
    outline: list[OutlineSection] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Harvest (step 4b)
# ---------------------------------------------------------------------------


class RawFact(_Strict):
    """One raw harvested span. Loose label, verbatim text, single page cite.

    text_span is allowed up to 2000 chars because long modal-phrased
    obligations can legitimately run several sentences. The categorise pass
    trims them; we don't want to lose recall at the harvest boundary.

    ``block_ids`` is a list of block-id strings (e.g. ``["b0123"]`` or
    ``["b0123","b0124"]``) referencing the blocks shown in the harvest
    prompt. Optional for backward compatibility with the legacy LLM-
    vision pipeline whose prompts don't surface block_ids — when
    populated, validate.py + load.py use them for deterministic bbox
    lookup via the geometry sidecar, bypassing the fuzzy snippet
    re-find subsystem.
    """
    id: str = Field(max_length=16)
    text_span: str = Field(max_length=2000)
    page_no: int = Field(ge=1)
    raw_label: str = Field(max_length=32)
    block_ids: list[str] = Field(
        max_length=20,
        description="Block IDs (e.g. 'b0123') from the harvest prompt that "
                    "this fact's text was lifted from. REQUIRED — the LLM "
                    "must emit this so downstream can do deterministic "
                    "bbox lookup via the geometry sidecar. Use an empty list "
                    "ONLY for facts that genuinely don't anchor to any block "
                    "(should be rare).",
    )


class HarvestResponse(_Strict):
    """Step 4b output: flat list of raw_facts."""
    raw_facts: list[RawFact] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Categorise (step 4c)
# ---------------------------------------------------------------------------


class FactSource(_Strict):
    """One mention of a canonical fact in the source document.

    ``snippet`` allows up to 2000 chars because defined_term definitions and
    long obligation/event spans can legitimately run several sentences. The
    snippet validator re-finds it on the page either way; a long snippet
    doesn't hurt downstream.
    """
    raw_fact_id: Optional[str] = None      # link back to a harvested fact, if known
    page_no: int = Field(ge=1)
    snippet: str = Field(max_length=2000)


class CanonicalFact(_Strict):
    """One de-duplicated, role-assigned fact written to canonical.json.

    Produced by ``normalise.py`` after per-category LLM normalisation and
    source aggregation. Each fact has a stable ``id``, a ``category`` from
    the analyzer's allowed list, a verbatim ``value``, a structured
    ``normalised`` payload (shape per ``ontology_compile.SPECS``), and a
    list of ``sources`` carrying page_no + snippet + bbox for citation.
    """
    id: str = Field(max_length=16)
    category: str = Field(description="One of the allowed categories")
    value: Any                              # verbatim source span
    normalised: dict[str, Any] = Field(default_factory=dict)
    role: Optional[str] = None              # None when no analyzer role matches
    sources: list[FactSource] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


# ---------------------------------------------------------------------------
# Normalise (step 4d — per-category LLM call)
# ---------------------------------------------------------------------------


class NormalisedFact(_Strict):
    """One bundled, normalised fact emitted by the per-category normalise pass.

    The ``normalised`` dict's shape depends on the category — see
    ``ontology_compile.SPECS`` for the per-category contract. We can't pin a
    Literal here because the field set differs per category; downstream
    validators (validate.py) read the dict directly.

    ``source_stub_ids`` lists the input stub ids that merged into this
    fact, so the orchestrator can reconstruct ``sources`` by aggregating
    the matching stubs' source lists.
    """
    id: str = Field(max_length=16)
    value: Any
    normalised: dict[str, Any] = Field(default_factory=dict)
    role: Optional[str] = None
    source_stub_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class NormaliseResponse(_Strict):
    """One per-category normalise response."""
    facts: list[NormalisedFact] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# OpenAI structured-output wrapper
# ---------------------------------------------------------------------------


def openai_json_schema(
    name: str, model: type[BaseModel], *, strict: bool = False,
) -> dict:
    """
    Wrap a pydantic model into the dict OpenAI expects in
    ``response_format={"type": "json_schema", "json_schema": <this>}``.
    """
    return {
        "name": name,
        "schema": model.model_json_schema(),
        "strict": strict,
    }
