"""store/model.py — the Cosmos DB document model (the Neo4j graph, flattened).

One container (``kb``), partition key ``/pk``:

  * doc-scoped items    pk = doc_id   (document, agreement, section, block,
                                       evidence, fact, mention, proposal,
                                       opsfield, docasset, edge)
  * global items        pk = "global" (identity hubs, whichever ones the
                                       active domain declares)
  * build metadata      pk = "meta"   (build stamp for the in-RAM vector index)

Design rules, carried over from the graph:

  * ``id`` is the natural key the graph used (fact_id, evidence_id, block_id,
    ops_id, ...) — globally unique already, so unique-per-partition for free.
  * 1-hop structural edges are NOT items — they live as fields on the child
    (evidence.fact_id/section_id/block_ids, block.section_id/order,
    fact.agreement_id, mention.fact_ids, opsfield.doc_id). The Neo4j NEXT
    chain becomes ``block.order`` (doc-wide reading-order index).
  * Edges that carry their own mutable properties (derived fact-to-fact
    links, HAS_PARTY/RESOLVES_TO, the document DAG) are ``edge`` items with
    a deterministic id, so re-runs upsert instead of duplicating.
  * Facts keep the dual-label idea as ``label`` (the type) + ``labels``
    (["Fact", label]) so "any fact of label X" stays one filter.
"""
from __future__ import annotations

from typing import Any

# --- kinds -----------------------------------------------------------------
DOCUMENT = "document"
AGREEMENT = "agreement"
SECTION = "section"
BLOCK = "block"
EVIDENCE = "evidence"
FACT = "fact"
MENTION = "mention"
PROPOSAL = "proposal"
OPSFIELD = "opsfield"
DOCASSET = "docasset"
EDGE = "edge"
HUB = "hub"          # sub-kind in `hub`: the domain's identity hub names
META = "meta"

GLOBAL_PK = "global"
META_PK = "meta"
BUILD_STAMP_ID = "build_stamp"

# Kinds that may carry an `embedding` (the three retrieval tiers).
EMBEDDED_KINDS = (EVIDENCE, SECTION, BLOCK)
EMBED_DIMS = 3072   # text-embedding-3-large


def item(kind: str, item_id: str, pk: str, props: dict[str, Any]) -> dict[str, Any]:
    """One Cosmos item: envelope + properties flattened at the top level.
    ``None`` values are dropped (mirrors the graph writers' ``_props``);
    dicts/lists are kept as-is — Cosmos stores JSON natively, so the Neo4j
    stringify-maps workaround does not apply here."""
    out: dict[str, Any] = {"id": item_id, "pk": pk, "kind": kind}
    for k, v in props.items():
        if v is not None and k not in ("id", "pk", "kind"):
            out[k] = v
    return out


def edge_item(rel: str, src: str, tgt: str, pk: str,
              props: dict[str, Any] | None = None) -> dict[str, Any]:
    """A property-carrying edge. Deterministic id → idempotent upsert."""
    return item(EDGE, f"e:{rel}:{src}:{tgt}", pk,
                {"rel": rel, "src": src, "tgt": tgt, **(props or {})})


def hub_item(hub_label: str, key: str, props: dict[str, Any]) -> dict[str, Any]:
    return item(HUB, f"{hub_label}:{key}", GLOBAL_PK,
                {"hub": hub_label, "key": key, **props})

