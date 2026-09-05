"""Retrieval tools exposed to the agent loop (Cosmos port).

Each tool is a small async function that takes typed kwargs and returns a
``list[Citation]``. The agent loop picks tools via OpenAI tool-calling.

Why a toolset instead of one vector query: the schema declares typed fact
labels with filterable properties, plus section- and block-level vector tiers.
A direct label lookup is exact and cheap, vector recall is the fallback when
intent is fuzzy. The agent chooses.

Every tool returns the same ``Citation`` shape so the loop can union them
into a single citation bundle keyed by ``evidence_id``.

This package used to be a single ``tools.py`` module. It is split into
cohesive submodules for readability:

  * ``_shared``, row/citation conversion, visibility checks, store fetch
    helpers, the embedding call. Used by every other submodule.
  * ``search``, vector_search_evidence / _sections / _blocks, keyword_search.
  * ``catalog``, corpus catalog text and document-identity blocks.
  * ``lookup``, section / defined-term lookup, cross-ref expansion, and
    the dormant human-correction net.
  * ``orphans``, last-resort orphan-fragment recall and document-asset
    (diagram/schematic) lookup.
  * ``ops``, the aligned/verified knowledge base: lookup_verified_fields,
    aggregate_ops_fields, and the pinned-family scope they share.
  * ``schemas``, OPENAI_TOOL_SCHEMAS handed to the agent loop.
  * ``dispatch``, TOOL_DISPATCH and tool-result serialisation.

This module re-exports the names other packages import, so
``from .tools import X`` / ``from ..rag.tools import X`` / ``from
api.rag.tools import X`` all keep working exactly as they did when this was
one file.
"""
from __future__ import annotations

from ._shared import (
    _CURRENCY_TAGS,
    _block_visible,
    _currency_prefixed,
    _row_to_citation,
    _span_visible,
    _text_cleared,
)
from .catalog import build_catalog_text, build_doc_titles, build_scope_docs_block
from .dispatch import TOOL_DISPATCH, serialize_tool_result
from .lookup import lookup_section
from .ops import aggregate_ops_fields, build_ops_scope, lookup_verified_fields
from .orphans import _orphan_is_substantive, _query_words, score_asset
from .schemas import OPENAI_TOOL_SCHEMAS

__all__ = [
    "OPENAI_TOOL_SCHEMAS",
    "TOOL_DISPATCH",
    "serialize_tool_result",
    "build_catalog_text",
    "build_doc_titles",
    "build_ops_scope",
    "build_scope_docs_block",
    "score_asset",
    "_CURRENCY_TAGS",
    "_currency_prefixed",
    "_text_cleared",
    "_block_visible",
    "_span_visible",
    "lookup_section",
    "_orphan_is_substantive",
    "_query_words",
    "_row_to_citation",
    "aggregate_ops_fields",
    "lookup_verified_fields",
]
