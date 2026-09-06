"""Graph endpoints for the inline subgraph + Explore tab.

* ``GET /graph/subgraph`` — given a set of evidence ids, return the nodes
  cited + their 1-hop neighbourhood (Section, Agreement, parent fact node,
  cross-references). Used by the chat panel under each answer.

* ``GET /graph/overview`` — paginated nodes + edges for the Explore tab.
  Filter by doctype / label / search term.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query

from pipeline.extraction.pack import load as load_pack

from .. import deps
from ..rag import policy as policy_mod
from ..rag.schemas import GraphEdge, GraphNode, GraphPayload
from .auth import current_user, require_clearance


router = APIRouter(prefix="/graph", tags=["graph"])

# An answer cites a handful of spans. The cap is generous and, unlike no cap,
# bounded: every id costs a row plus follow-up lookups.
_MAX_SUBGRAPH_IDS = 100


# ---------------------------------------------------------------------------
# Subgraph for a chat answer
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _label_to_key() -> dict[str, str]:
    """Item label → primary-key property name, for stable graph node ids in the
    frontend.

    Read from the active pack, which already declares a `key` for every
    structural node type and every fact type. This used to be a literal dict of
    one domain's labels, so a domain with different fact types had its nodes
    silently dropped here: no key, no id, no node.
    """
    pack = load_pack()
    keys = dict(pack.node_keys)
    for label in pack.fact_labels:
        k = pack.id_key(label)
        if k:
            keys[label] = k
    return keys


def _humanize_action(s: str | None) -> str | None:
    """Turn snake_case canonical_action into a readable phrase."""
    if not s:
        return None
    return s.replace("_", " ").strip()


@lru_cache(maxsize=1)
def _summary_keys_by_label() -> dict[str, tuple[str, ...]]:
    """Each fact label's display projection, from the ACTIVE pack. Same source
    the retrieval layer's `_fact_summary` reads, so a node reads the same way
    in the graph as it does in a citation."""
    pack = load_pack()
    return {lab: tuple(pack.summary_keys(lab)) for lab in pack.fact_labels}


def _pack_summary(label: str, props: dict) -> str:
    """Render a fact from its pack-declared summary keys, skipping blanks."""
    parts: list[str] = []
    for key in _summary_keys_by_label().get(label, ()):
        val = props.get(key)
        if val is None or val == "":
            continue
        text = str(val)
        parts.append(text.replace("_", " ").strip() if "_" in text else text)
    return " · ".join(parts)


def _readable_title(label: str, props: dict) -> str:
    """Human-readable label for a graph node.

    Cryptic IDs (doc_id hashes, fact_ids) MUST NOT appear. The user sees
    plain English: section numbers with titles, party names + roles,
    obligations as verb phrases, charges with amounts, etc.
    """
    def trunc(s: str, n: int = 110) -> str:
        s = (s or "").strip()
        return s if len(s) <= n else s[: n - 1] + "…"

    if label == "Section":
        num = props.get("section_num") or ""
        ttl = props.get("title") or ""
        if num and ttl:   return f"§{num} — {ttl}"
        if num:           return f"§{num}"
        return ttl or "Section"

    if label == "Agreement":
        return trunc(props.get("title") or props.get("agreement_type") or "Agreement")

    if label == "Document":
        return trunc(props.get("doctype") or "Document")

    if label == "EvidenceSpan":
        return trunc(props.get("text_span") or "Evidence", 80)

    if label == "Obligation":
        modal = props.get("modality") or "must"
        actor = props.get("actor_role") or ""
        verb  = _humanize_action(props.get("canonical_action") or props.get("action")) or "act"
        obj   = props.get("object") or ""
        parts = [actor, modal, verb, obj]
        return trunc(" ".join(p for p in parts if p))

    if label == "Right":
        holder = props.get("holder_role") or ""
        modal  = props.get("modality") or "may"
        verb   = _humanize_action(props.get("canonical_action") or props.get("action")) or "act"
        obj    = props.get("object") or ""
        parts = [holder, modal, verb, obj]
        return trunc(" ".join(p for p in parts if p))

    if label == "Date":
        dt   = props.get("date_text") or props.get("date_value") or ""
        kind = props.get("date_type") or ""
        if dt and kind and kind != "other":
            return f"{_humanize_action(kind)}: {dt}"
        return str(dt) or _humanize_action(kind) or "Date"

    if label == "Party":
        name = props.get("name") or ""
        role = props.get("role")
        if role and role != "null":
            return f"{name} ({role})"
        return name or "Party"

    if label == "Location":
        return props.get("address") or props.get("name") or "Location"

    if label == "Condition":
        return trunc(props.get("condition_text") or "Condition", 90)

    if label == "Event":
        name = props.get("name") or ""
        et   = props.get("event_type") or ""
        if et and et != "other":
            return f"{name or _humanize_action(et)}"
        return name or "Event"

    if label == "DefinedTerm":
        term = props.get("term") or ""
        return f'"{term}"' if term else "Defined term"

    if label == "Schedule":
        num = props.get("schedule_num") or ""
        ttl = props.get("title") or ""
        if num and ttl:   return f"{num} — {ttl}"
        return num or ttl or "Schedule"

    # Every label above is part of the structural spine in `_base.yaml`, so
    # every domain has them. Anything else is the DOMAIN's own label, and the
    # pack already declares how to summarise one. Six per-label formatters used
    # to sit here instead, all written for the first domain's labels, so a
    # label they did not name fell through to the raw `name` property: a
    # Payment node rendered as `license_fee`, with no amount and no currency.
    summary = _pack_summary(label, props)
    if summary:
        return trunc(summary)
    return props.get("name") or props.get("title") or label


# Internal property keys we strip from the payload sent to the frontend.
# Cryptic <doc_id>:* IDs and embedding vectors are noise; the front-end
# already has its own opaque node id for routing.
_HIDE_PROPS = {
    "embedding", "embed_text", "raw_fact_id", "agreement_id",
    "doc_id", "extraction_version",
    # Cosmos envelope + bookkeeping fields.
    "id", "pk", "kind", "label", "labels", "hub", "ext",
    "_rid", "_self", "_etag", "_attachments", "_ts",
}
# These end in _id but are USEFUL to keep — section_num gives a friendlier
# version; hide nothing else here.


_KIND_TO_LABEL = {
    "document": "Document", "agreement": "Agreement", "section": "Section",
    "evidence": "EvidenceSpan",
}


def _labels_of(item: dict | None) -> list[str]:
    """The item's graph-era label set: facts carry `labels`, structural
    kinds map 1:1."""
    if not item:
        return []
    if item.get("kind") == "fact":
        return list(item.get("labels") or [])
    lab = _KIND_TO_LABEL.get(item.get("kind") or "")
    return [lab] if lab else []


def _node_to_payload(item: dict | None) -> GraphNode | None:
    """Convert a store item to a GraphNode. Skip items whose label isn't
    one we know how to key."""
    if item is None:
        return None
    label_keys = _label_to_key()
    labels = _labels_of(item)
    primary = next((l for l in labels if l in label_keys), None)
    if primary is None:
        return None
    key = label_keys[primary]
    nid = item.get(key)
    if not nid:
        return None

    raw = dict(item)
    # The frontend never needs the cryptic *_id primary keys in the props —
    # the GraphNode.id field is the routing key. Strip them all.
    pretty: dict = {}
    for k, v in raw.items():
        if k in _HIDE_PROPS:                  continue
        if k.endswith("_id"):                 continue
        if v is None or v == "" or v == "null": continue
        pretty[k] = v

    return GraphNode(
        id=nid,
        label=primary,
        title=_readable_title(primary, raw)[:140],
        props=pretty,
    )


def _hub_to_payload(item: dict | None) -> GraphNode | None:
    """Convert an identity hub item to a graph node.

    Hubs are not typed facts, so they sit outside `_label_to_key()`. They need
    no per-hub map either: `model.hub_item` writes the normalised value as
    `key` on every hub, whatever the domain calls it, and a display `name` when
    the writer has one. The id is namespaced so it cannot collide with a fact
    id that happens to share the value.
    """
    if item is None:
        return None
    label = item.get("hub")
    if label not in load_pack().hubs:
        return None
    nid = item.get("key")
    if not nid:
        return None
    pretty = {
        k: v for k, v in item.items()
        if k not in _HIDE_PROPS and v not in (None, "", "null")
    }
    title = str(item.get("name") or nid)
    return GraphNode(
        id=f"hub:{nid}", label=label, title=title[:140], props=pretty,
    )


def _clean_edge_props(props: dict | None) -> dict:
    """Keep only explainability-relevant edge props; coerce to JSON-safe."""
    if not props:
        return {}
    out: dict = {}
    for k, v in props.items():
        if k in ("id", "pk", "kind", "src", "tgt", "rel") or k.startswith("_"):
            continue
        if v in (None, "", "null"):
            continue
        out[k] = v
    return out


@router.get("/legend", dependencies=[Depends(require_clearance)])
async def legend() -> dict:
    """How the Explore graph groups and colours what it draws.

    Served from the active pack rather than written into the frontend. The
    frontend used to carry its own copy of one domain's labels, which meant a
    domain with different fact types drew them all in the unclassified grey
    while the legend named labels that did not exist.

    ``groups`` is the legend in display order. ``labels`` maps every node label
    the graph can emit to one of those group keys.
    """
    pack = load_pack()
    return {"groups": [dict(g) for g in pack.ui_groups],
            "labels": dict(pack.ui_group_for)}


@router.get("/subgraph", response_model=GraphPayload)
async def subgraph(
    evidence_ids: list[str] = Query(default=[], alias="evidence_ids"),
    user: dict = Depends(current_user),
) -> GraphPayload:
    """Build a small per-answer graph anchored on the cited evidence.

    Chat furniture — available to every logged-in user, so it enforces the
    viewer's access policy itself: fact nodes with a restricted label (and the
    evidence spans that only those facts back) are dropped for uncleared roles.
    Without this, a default user could feed arbitrary evidence ids and read
    Charge/Rate values out of `fact_props`."""
    if not evidence_ids:
        return GraphPayload(nodes=[], edges=[])
    # Cap the input. It is caller-supplied and was unbounded, and every id costs
    # a row on an embedding-bearing kind plus follow-up lookups. An answer cites
    # a handful of spans, so this is far above anything real.
    if len(evidence_ids) > _MAX_SUBGRAPH_IDS:
        raise HTTPException(
            400, f"too many evidence_ids (limit {_MAX_SUBGRAPH_IDS})")
    restricted = policy_mod.restricted_labels(user.get("role"))
    store = deps.get_store()
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []

    def _is_restricted(item: dict | None) -> bool:
        return bool(item is not None and restricted
                    and restricted.intersection(_labels_of(item)))

    # `evidence` is an embedding-bearing kind, so `SELECT *` here dragged a
    # 3072-float vector per row across the wire for fields nobody reads. Project
    # what the payload actually needs. Invisible on the local emulator, several
    # megabytes per request on live Azure.
    evs = await store.query(
        "SELECT c.id, c.doc_id, c.kind, c.label, c.labels, c.section_id, "
        "c.target_section_id, c.fact_id, c.agreement_id, c.page_no, "
        "c.text_span, c.snippet, c.bbox_x0, c.bbox_y0, c.bbox_x1, c.bbox_y1 "
        "FROM c WHERE c.kind = 'evidence' AND ARRAY_CONTAINS(@ids, c.id)",
        [{"name": "@ids", "value": list(evidence_ids)}])
    if not evs:
        return GraphPayload(nodes=[], edges=[])

    async def _points(pairs: set[tuple[str, str]]) -> dict[str, dict]:
        """Batched by partition. This used to be one sequential `store.point`
        per id, called four times over, so a single request could issue
        hundreds of round trips against a single-worker server."""
        by_pk: dict[str, list[str]] = defaultdict(list)
        for item_id, pk in pairs:
            by_pk[pk].append(item_id)
        if not by_pk:
            return {}
        results = await asyncio.gather(
            *[store.fetch_many(ids, pk=pk) for pk, ids in by_pk.items()])
        out: dict[str, dict] = {}
        for chunk in results:
            out.update(chunk)
        return out

    # One wave rather than four sequential ones: the four sets are independent.
    sections, targets, facts, agreements = await asyncio.gather(
        _points({(e["section_id"], e["doc_id"]) for e in evs
                 if e.get("section_id")}),
        _points({(e["target_section_id"], e["doc_id"]) for e in evs
                 if e.get("target_section_id")}),
        _points({(e["fact_id"], e["doc_id"]) for e in evs if e.get("fact_id")}),
        _points({(e["agreement_id"], e["doc_id"]) for e in evs
                 if e.get("agreement_id")}),
    )

    # Add nodes (restricted-label facts never enter the payload). Track which
    # spans are backed ONLY by restricted facts — those spans carry the
    # restricted quote verbatim and must drop with their fact.
    span_restricted: dict[str, bool] = {}
    for e in evs:
        e_node = _node_to_payload(e)
        if e_node is None:
            continue
        nodes[e_node.id] = e_node
        fact = facts.get(e.get("fact_id"))
        if fact is not None:
            bad = _is_restricted(fact)
            span_restricted[e_node.id] = span_restricted.get(e_node.id, True) and bad
            if not bad:
                n_node = _node_to_payload(fact)
                if n_node is not None:
                    nodes[n_node.id] = n_node
                    edges.append(GraphEdge(
                        id=f"{n_node.id}->{e_node.id}:SUPPORTED_BY",
                        source=n_node.id, target=e_node.id, type="SUPPORTED_BY",
                    ))
                    a_node = _node_to_payload(agreements.get(e.get("agreement_id")))
                    if a_node is not None:
                        nodes[a_node.id] = a_node
                        edges.append(GraphEdge(
                            id=f"{a_node.id}->{n_node.id}:HAS_FACT",
                            source=a_node.id, target=n_node.id, type="HAS_FACT",
                        ))
        s_node = _node_to_payload(sections.get(e.get("section_id")))
        if s_node is not None:
            nodes[s_node.id] = s_node
            edges.append(GraphEdge(
                id=f"{e_node.id}->{s_node.id}:IN_SECTION",
                source=e_node.id, target=s_node.id, type="IN_SECTION",
            ))
        t_node = _node_to_payload(targets.get(e.get("target_section_id")))
        if t_node is not None:
            nodes[t_node.id] = t_node
            edges.append(GraphEdge(
                id=f"{e_node.id}->{t_node.id}:REFERENCES_SECTION",
                source=e_node.id, target=t_node.id, type="REFERENCES_SECTION",
            ))

    # Drop spans whose every backing fact was restricted (the span text IS the
    # restricted quote), then dedup edges and prune ones touching dropped nodes.
    for span_id, only_restricted in span_restricted.items():
        if only_restricted:
            nodes.pop(span_id, None)
    seen = set()
    unique_edges = []
    for e in edges:
        if e.id in seen or e.source not in nodes or e.target not in nodes:
            continue
        seen.add(e.id)
        unique_edges.append(e)
    return GraphPayload(nodes=list(nodes.values()), edges=unique_edges)


# ---------------------------------------------------------------------------
# Overview for the Explore tab
# ---------------------------------------------------------------------------
#
# Unlike the per-answer subgraph (which is an evidence tree), the Explore view
# is a *real* knowledge graph: the document spine, typed facts grouped to the
# section they live in, the cross-document identity hubs the KB resolves
# mentions onto, and the deterministic fact-to-fact edges. Everything the
# react-flow Explore canvas needs to render + explain is folded into this one
# payload so the UI never has to guess topology or re-fetch per click.


@lru_cache(maxsize=1)
def _derived_fact_edges() -> list[str]:
    """Fact-to-fact edges the Explore graph surfaces, read from the ACTIVE
    pack. Still a closed allow-list, so an edge the ontology never declared is
    never rendered, but the list is now the domain's own.

    This was a literal copy of the first domain's answer, which meant every
    other domain lost the edges it does declare and the canvas quietly named
    several that do not exist.
    """
    return sorted(load_pack().derived_edges)


def _proposal_to_payload(p) -> GraphNode | None:
    """Proposals (quarantined / unverified) have their own id key + no entry
    in _LABEL_TO_KEY (they're not facts). Surface them so the UI can flag
    trust state, but keep them visually distinct from asserted nodes."""
    if p is None:
        return None
    raw = dict(p)
    pid = raw.get("proposal_id")
    if not pid:
        return None
    pretty = {
        k: v for k, v in raw.items()
        if k not in _HIDE_PROPS and not k.endswith("_id")
        and v not in (None, "", "null")
    }
    kind = raw.get("proposal_kind") or "proposal"
    return GraphNode(
        id=pid, label="Proposal",
        title=f"Proposal · {kind}"[:140], props=pretty,
    )


@router.get("/overview", response_model=GraphPayload,
            dependencies=[Depends(require_clearance)])
async def overview(
    doc_id: str | None = Query(default=None),
    group: str | None = Query(default=None),
    labels: list[str] = Query(default=[]),
    limit_nodes: int = Query(default=200, ge=10, le=600),
) -> GraphPayload:
    """High-level knowledge graph for the Explore tab.

    Returns, capped at ``limit_nodes`` facts:
      * the document spine — Document → Agreement → Section
      * typed fact nodes attached to their Agreement (HAS_FACT) AND grouped
        to the Section they were extracted in (IN_SECTION, derived through
        the EvidenceSpan spine) — with provenance (page_no + a representative
        evidence_id + snippet) folded onto the fact node, so the UI can open
        the PDF highlight without a second round-trip
      * identity hubs (whatever the active pack declares) via RESOLVES_TO,
        with method / confidence / signals carried as edge props
      * the document-level DAG — AMENDS / SUPERSEDES / NOVATES, the uploader's
        declared relation between contracts (pipeline/kb/intake.py), already
        computed for chain-role resolution but never drawn here before
      * deterministic fact-to-fact edges (CONDITIONED_ON, TRIGGERED_BY, …)
      * quarantined :Proposal nodes (flagged so the UI can mark them unverified)

    Scope is one document (``doc_id``), one contract family (``group`` — the
    same value an amendment shares with the base it amends), or every loaded
    contract when both are null, which is the whole point of the identity
    hubs. ``doc_id`` wins if both are given.
    """
    store = deps.get_store()
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    seen_edges: set[str] = set()

    def add_edge(src: str, tgt: str, etype: str, props: dict | None = None) -> None:
        eid = f"{src}->{tgt}:{etype}"
        if eid in seen_edges:
            return
        seen_edges.add(eid)
        edges.append(GraphEdge(
            id=eid, source=src, target=tgt, type=etype, props=props or {},
        ))

    # Default to every fact label the ACTIVE pack declares. A literal list here
    # meant the overview silently omitted any fact type the first domain did
    # not have, and queried for several the current domain does not have.
    label_filter = list(labels) if labels else sorted(load_pack().fact_labels)
    label_set = list({l for l in label_filter})

    # A single doc, a whole family (every doc sharing one `group`, per
    # pipeline/kb/intake.py), or every document (both unset).
    doc_ids: list[str] | None = None
    if doc_id:
        doc_ids = [doc_id]
    elif group:
        rows = await store.query(
            # Bracket, not dot: GROUP is a Cosmos SQL reserved word (same
            # reason `c["order"]` is used elsewhere in this file) — `c.group`
            # parses but silently fails to filter, matching every document.
            "SELECT c.doc_id FROM c WHERE c.kind = 'document' AND c[\"group\"] = @g",
            [{"name": "@g", "value": group}])
        # A family with no members left is a real empty result, not "give me
        # everything" — @docs on an empty list matches nothing, correctly.
        doc_ids = [r["doc_id"] for r in rows]

    scope_sql = "true" if doc_ids is None else "ARRAY_CONTAINS(@docs, c.doc_id)"

    def _params() -> list[dict]:
        return [] if doc_ids is None else [{"name": "@docs", "value": doc_ids}]

    # 1) One concurrent wave per independent kind. Everything below resolves
    #    per-item lookups from these in-RAM maps: a store.point is a full
    #    network round trip on real Azure (the emulator's sub-ms localhost
    #    reads hid that), and this view used to fire hundreds of them.
    agrs, doc_rows, sec_rows, facts, proposals = await asyncio.gather(
        store.query(
            f"SELECT * FROM c WHERE c.kind = 'agreement' AND {scope_sql}", _params()),
        store.query(
            f"SELECT * FROM c WHERE c.kind = 'document' AND {scope_sql}", _params()),
        # Sections are an EMBEDDED kind (store/model.EMBEDDED_KINDS): SELECT *
        # drags a 3072-float vector + the embed text per row (~30 MB for the
        # full corpus, seconds from real Azure), so project the display fields.
        # c["order"] because ORDER is a reserved word.
        store.query(
            "SELECT c.id, c.kind, c.doc_id, c.section_id, c.section_num, "
            "c.section_kind, c.title, c[\"order\"], c.page_start, c.page_end, "
            "c.bbox_x0, c.bbox_y0, c.bbox_x1, c.bbox_y1 "
            f"FROM c WHERE c.kind = 'section' AND {scope_sql}", _params()),
        store.query(
            f"SELECT TOP {int(limit_nodes)} * FROM c WHERE c.kind = 'fact' "
            f"AND ARRAY_CONTAINS(@labs, c.label) AND {scope_sql}",
            [{"name": "@labs", "value": label_set}, *_params()]),
        store.query(
            f"SELECT * FROM c WHERE c.kind = 'proposal' AND {scope_sql}", _params()),
    )

    # Document spine — Document → Agreement → Section.
    docs = {d["doc_id"]: d for d in doc_rows}
    sections_by_doc: dict[str, list[dict]] = defaultdict(list)
    sec_by_id: dict[str, dict] = {}
    for s in sec_rows:
        sections_by_doc[s["doc_id"]].append(s)
        sec_by_id[s["id"]] = s
    for a_item in agrs:
        a = _node_to_payload(a_item)
        if a is None:
            continue
        nodes[a.id] = a
        d = _node_to_payload(docs.get(a_item["doc_id"]))
        if d is not None:
            # A Document's own title is its doctype ("commercial_agreement"),
            # identical across every document of one domain — invisible in a
            # single-document view, but every box in a multi-document family
            # view reads the same, indistinguishable at a glance. Its Agreement
            # is always exactly one (this pipeline never writes more than one
            # per document) and carries the real, per-contract title straight
            # from extraction rather than the optional intake sidecar, so
            # borrow it.
            d.title = a.title
            nodes[d.id] = d
            add_edge(d.id, a.id, "CONTAINS_AGREEMENT")
        secs = sorted(sections_by_doc.get(a_item["doc_id"], []),
                      key=lambda s: s.get("order") or 0)[:60]
        for s in secs:
            gs = _node_to_payload(s)
            if gs:
                nodes[gs.id] = gs
                add_edge(a.id, gs.id, "HAS_SECTION")

    # 2) Typed facts + provenance + their grouping section, resolved through
    #    the evidence (fact_id / section_id fields), with a representative
    #    page/snippet folded onto the fact so the front-end can jump to the
    #    PDF highlight directly.
    fact_by_id = {f["id"]: f for f in facts}
    evs_by_fact: dict[str, list[dict]] = defaultdict(list)
    if facts:
        for e in await store.query(
                "SELECT c.fact_id, c.id, c.doc_id, c.page_no, c.text_span, "
                "c.section_id FROM c WHERE c.kind = 'evidence' AND "
                "ARRAY_CONTAINS(@fids, c.fact_id)",
                [{"name": "@fids", "value": list(fact_by_id)}]):
            evs_by_fact[e["fact_id"]].append(e)
    for f in facts:
        gn = _node_to_payload(f)
        a_id = f.get("agreement_id")
        if gn is None or a_id not in nodes:
            continue
        evs = sorted(evs_by_fact.get(f["id"], []),
                     key=lambda e: e.get("page_no") or 0)
        e0 = evs[0] if evs else None
        if e0 is not None:
            gn.props["evidence_id"] = e0["id"]
            if e0.get("page_no") is not None:
                gn.props["page_no"] = e0["page_no"]
            if e0.get("text_span"):
                gn.props["snippet"] = str(e0["text_span"])[:280]
            gn.props["n_evidence"] = len(evs)
        nodes[gn.id] = gn
        add_edge(a_id, gn.id, "HAS_FACT")
        sid = (e0 or {}).get("section_id")
        if sid:
            gs = _node_to_payload(sec_by_id.get(sid))
            if gs is not None:
                nodes.setdefault(gs.id, gs)
                add_edge(gs.id, gn.id, "IN_SECTION")

    fact_ids = [nid for nid, gn in nodes.items() if gn.label in label_set]

    # 3+4) Fact-to-fact edges and RESOLVES_TO hub edges (independent, one
    #      wave), then the hub nodes themselves (one wave) — all batched,
    #      no per-item reads.
    hub_edges: list[dict] = []
    if fact_ids:
        edge_rows, hub_edges = await asyncio.gather(
            store.query(
                "SELECT * FROM c WHERE c.kind = 'edge' AND "
                "ARRAY_CONTAINS(@rels, c.rel) AND ARRAY_CONTAINS(@ids, c.src)",
                [{"name": "@rels", "value": _derived_fact_edges()},
                 {"name": "@ids", "value": fact_ids}]),
            store.query(
                "SELECT c.src, c.tgt FROM c WHERE c.kind = 'edge' AND "
                "c.rel = 'RESOLVES_TO' AND ARRAY_CONTAINS(@ids, c.src)",
                [{"name": "@ids", "value": fact_ids}]),
        )
        for e in edge_rows:
            if e["tgt"] in nodes:
                add_edge(e["src"], e["tgt"], e["rel"], _clean_edge_props(e))

    hub_ids = sorted({e["tgt"] for e in hub_edges})
    if hub_ids:
        hubs_map = await store.fetch_many(hub_ids, pk="global")
        for e in hub_edges:
            ghub = _hub_to_payload(hubs_map.get(e["tgt"]))
            if ghub is None:
                continue
            nodes.setdefault(ghub.id, ghub)
            add_edge(e["src"], ghub.id, "RESOLVES_TO")

    # 4.5) Document-level DAG — declared AMENDS/SUPERSEDES/NOVATES between
    #      documents (the uploader's relation at intake, materialised by
    #      build_km for chain-role resolution). Computed already; never drawn
    #      here before, so a corpus that IS a set of amendment chains rendered
    #      as disconnected documents with no explanation of how they relate.
    #      An endpoint outside the current scope (e.g. viewing just one
    #      amendment, not its base) is pulled in so the edge has somewhere
    #      to land.
    doc_node_ids = [nid for nid, gn in nodes.items() if gn.label == "Document"]
    if doc_node_ids:
        dag_edges = await store.query(
            "SELECT * FROM c WHERE c.kind = 'edge' AND "
            "ARRAY_CONTAINS(['AMENDS', 'SUPERSEDES', 'NOVATES'], c.rel) AND "
            "(ARRAY_CONTAINS(@ids, c.src) OR ARRAY_CONTAINS(@ids, c.tgt))",
            [{"name": "@ids", "value": doc_node_ids}])
        missing = sorted({d for e in dag_edges for d in (e["src"], e["tgt"])} - set(nodes))
        if missing:
            extra_docs = await store.query(
                "SELECT * FROM c WHERE c.kind = 'document' AND "
                "ARRAY_CONTAINS(@ids, c.doc_id)",
                [{"name": "@ids", "value": missing}])
            for d_item in extra_docs:
                gd = _node_to_payload(d_item)
                if gd is not None:
                    nodes.setdefault(gd.id, gd)
        for e in dag_edges:
            if e["src"] in nodes and e["tgt"] in nodes:
                add_edge(e["src"], e["tgt"], e["rel"], _clean_edge_props(e))

    # 5) Quarantined proposals (per-document; flagged unverified in the UI).
    by_doc: dict[str, list[dict]] = defaultdict(list)
    for p in proposals:
        if p.get("doc_id"):
            by_doc[p["doc_id"]].append(p)
    for did, plist in by_doc.items():
        for p in plist[:40]:
            gp = _proposal_to_payload(p)
            if gp is None:
                continue
            nodes.setdefault(gp.id, gp)
            # Anchor the proposal to its document if that doc is in view.
            if did in nodes and nodes[did].label == "Document":
                add_edge(did, gp.id, "HAS_PROPOSAL")

    return GraphPayload(nodes=list(nodes.values()), edges=edges)


# ---------------------------------------------------------------------------
# Cross-doc hub lens — the identity story, nothing else
# ---------------------------------------------------------------------------
#
# Where /overview is the whole graph (700+ nodes), the lens is the *point* of
# the cross-document KB distilled: which documents name the same real-world
# entity (Document → canonical hub via resolved facts). No sections, no
# intra-doc facts.
#
# It has its OWN endpoint (not a filter of /overview) so it is never clipped
# by the overview fact cap — a hub that spans two contracts must always show
# both, regardless of how many other facts those contracts have.


@router.get("/hubs", response_model=GraphPayload,
            dependencies=[Depends(require_clearance)])
async def hubs(doc_id: str | None = Query(default=None),
               group: str | None = Query(default=None)) -> GraphPayload:
    """The cross-document identity lens.

    Nodes: Documents and the identity hubs their facts resolve to. Edges:
    ``HAS_ENTITY`` (Document → hub, with a fact count and a few sample names, a
    derived display edge). Scope is one document (``doc_id``), one contract
    family (``group``), or every document when both are null, which is the
    whole reason the lens exists.

    It used to carry a second layer, grounding each hub onto an external
    customer/block register. Those registers were one deployment's master data
    rather than part of the framework and have been retired, so the lens is now
    identity only.
    """
    store = deps.get_store()
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    seen_edges: set[str] = set()

    def add_edge(src: str, tgt: str, etype: str, props: dict | None = None) -> None:
        eid = f"{src}->{tgt}:{etype}"
        if eid in seen_edges:
            return
        seen_edges.add(eid)
        edges.append(GraphEdge(
            id=eid, source=src, target=tgt, type=etype, props=props or {},
        ))

    doc_ids: list[str] | None = None
    if doc_id:
        doc_ids = [doc_id]
    elif group:
        rows = await store.query(
            # Bracket, not dot — see the matching comment in overview() above.
            "SELECT c.doc_id FROM c WHERE c.kind = 'document' AND c[\"group\"] = @g",
            [{"name": "@g", "value": group}])
        doc_ids = [r["doc_id"] for r in rows]

    # One concurrent wave for every edge lens + the documents, then one
    # batched wave for the items those edges reference. Per-item point reads
    # are a full round trip each on real Azure — never loop them here.
    def _doc_params() -> list[dict]:
        return [] if doc_ids is None else [{"name": "@docs", "value": doc_ids}]

    resolve_edges, doc_rows = await asyncio.gather(
        store.query(
            "SELECT c.pk, c.src, c.tgt FROM c WHERE c.kind = 'edge' AND "
            "c.rel = 'RESOLVES_TO'"
            + (" AND ARRAY_CONTAINS(@docs, c.pk)" if doc_ids is not None else ""),
            _doc_params()),
        store.query("SELECT * FROM c WHERE c.kind = 'document'"),
    )
    doc_cache = {d["id"]: d for d in doc_rows}
    hub_ids = sorted({e["tgt"] for e in resolve_edges})
    hubs_map, fact_cache = await asyncio.gather(
        store.fetch_many(hub_ids, pk="global"),
        # Only the display-name fields — facts can be numerous here.
        store.fetch_many(sorted({e["src"] for e in resolve_edges}),
                         select=["name", "term", "address"]),
    )

    # Documents → identity hubs (via the RESOLVES_TO edge items, whose pk IS
    # the citing doc), with how many facts map to the hub + a few names.
    per_pair: dict[tuple[str, str], dict] = {}
    for e in resolve_edges:
        f = fact_cache.get(e["src"])
        slot = per_pair.setdefault((e["pk"], e["tgt"]), {"n": 0, "names": []})
        slot["n"] += 1
        name = (f or {}).get("name") or (f or {}).get("term") or (f or {}).get("address")
        if name and name not in slot["names"] and len(slot["names"]) < 3:
            slot["names"].append(name)
    for (did, hid), agg in per_pair.items():
        d = _node_to_payload(doc_cache.get(did))
        hub = _hub_to_payload(hubs_map.get(hid))
        if d is None or hub is None:
            continue
        nodes[d.id] = d
        nodes.setdefault(hub.id, hub)
        add_edge(d.id, hub.id, "HAS_ENTITY", {
            "facts": agg["n"], "names": agg["names"],
        })

    return GraphPayload(nodes=list(nodes.values()), edges=edges)
