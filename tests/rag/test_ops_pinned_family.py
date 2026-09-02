"""Pinned amendment inherits, with a label — the OPS-layer family expansion.

When the user pins document(s) in the UI, every tool is scoped to them. But
an amendment only re-states the fields it changes: the rest live on family
siblings as their is_current rows. The OPS tools therefore expand a pinned
set to the pinned docs' families, admit ONLY current rows from unpinned
siblings, and mark them carried-over. Raw-text and fact tools stay strictly
pinned (verified via the dispatch seam).
"""
from __future__ import annotations

import asyncio

import pytest

from api.rag import agent, tools


class _FakeStore:
    """Answers the document, opsfield and id-fetch queries the ops tools make."""

    def __init__(self, docs, opsfields):
        self._docs = list(docs)         # full document items (id == doc_id)
        self._ops = list(opsfields)

    async def query(self, sql, params=None, pk=None):
        p = {x["name"]: x["value"] for x in (params or [])}
        if "ARRAY_CONTAINS(@ids, c.id)" in sql:
            kind = p.get("@kind")
            pool = self._docs + self._ops
            return [it for it in pool if it["id"] in p["@ids"]
                    and (kind is None or it.get("kind") == kind)]
        if "DISTINCT" in sql and "'opsfield'" in sql:
            seen, out = set(), []
            for o in self._ops:
                if o["field_key"] not in seen:
                    seen.add(o["field_key"])
                    out.append({"field_key": o["field_key"],
                                "title": o.get("title")})
            return out
        if "'opsfield'" in sql:
            rows = self._ops
            if "@fk" in p:
                rows = [o for o in rows if o.get("field_key") == p["@fk"]]
            scope = p.get("@scope_ids")
            if scope is not None:
                rows = [o for o in rows if o["doc_id"] in scope]
            return rows
        if "c.kind = 'document'" in sql:
            return [{"doc_id": d["doc_id"], "title": d.get("title"),
                     "grp": d.get("group")} for d in self._docs]
        return []


def _doc(doc_id, title, group):
    return {"id": doc_id, "doc_id": doc_id, "kind": "document",
            "title": title, "group": group, "effective_date": "2020-01-01"}


def _ops(ops_id, doc_id, field_key, title, value, *, current=True, numbers=()):
    return {"id": ops_id, "ops_id": ops_id, "kind": "opsfield",
            "doc_id": doc_id, "field_key": field_key,
            "full_key": f"cat.{field_key}", "title": title,
            "category": "commercial_link", "value": value,
            "snippet": f"the {title} is {value}", "page": 1,
            "numbers": list(numbers), "unit": None,
            "trust": "machine_extracted", "verified": False,
            "disputed": False, "is_current": current,
            "sensitivity": "general"}


_DOCS = [
    _doc("docA", "Base Agreement", "fam1"),
    _doc("docA2", "Second Supplemental", "fam1"),
    _doc("docB", "Other Family Agreement", "fam2"),
]

_OPS = [
    # Re-stated by the pinned amendment: current there, superseded in the base.
    _ops("o1", "docA2", "supply_temp", "Supply Temperature", "6.5 °C"),
    _ops("o3", "docA", "supply_temp", "Supply Temperature", "7.0 °C",
         current=False),
    # Never re-stated: lives current on the base, inherits into the pin.
    _ops("o2", "docA", "energy_charge", "Energy Charge Fees", "0.58",
         numbers=[0.58]),
    # Another family: never admitted.
    _ops("o4", "docB", "energy_charge", "Energy Charge Fees", "0.99",
         numbers=[0.99]),
]


def _scope(store, pinned):
    return asyncio.run(tools.build_ops_scope(store, pinned))


def test_build_ops_scope_expands_to_the_family():
    scope = _scope(_FakeStore(_DOCS, _OPS), ["docA2"])
    assert scope["pinned"] == ["docA2"]
    assert set(scope["doc_ids"]) == {"docA2", "docA"}
    assert scope["titles"]["docA"] == "Base Agreement"


def test_lookup_admits_current_sibling_rows_with_carried_marker():
    store = _FakeStore(_DOCS, _OPS)
    scope = _scope(store, ["docA2"])
    cites = asyncio.run(tools.lookup_verified_fields(
        store=store, query="supply temperature energy charge",
        role="admin", doc_ids=["docA2"], ops_scope=scope))
    by_id = {c.evidence_id: c for c in cites}
    # The amendment's own row: in, no marker.
    assert "carried over" not in by_id["ops:o1"].fact_summary
    # The base's un-restated field: in, marked.
    assert ("carried over from Base Agreement (unchanged by the pinned document)"
            in by_id["ops:o2"].fact_summary)
    # The base's superseded statement and the other family: out.
    assert "ops:o3" not in by_id
    assert "ops:o4" not in by_id


def test_superseded_sibling_rows_stay_out_even_with_include_superseded():
    store = _FakeStore(_DOCS, _OPS)
    scope = _scope(store, ["docA2"])
    cites = asyncio.run(tools.lookup_verified_fields(
        store=store, query="supply temperature energy charge",
        role="admin", include_superseded=True, ops_scope=scope))
    assert "ops:o3" not in {c.evidence_id for c in cites}


def test_unpinned_lookup_is_unchanged():
    store = _FakeStore(_DOCS, _OPS)
    cites = asyncio.run(tools.lookup_verified_fields(
        store=store, query="energy charge", role="admin"))
    assert all("carried over" not in (c.fact_summary or "") for c in cites)


def test_aggregate_over_expanded_set_is_current_only_and_marked():
    store = _FakeStore(_DOCS, _OPS)
    scope = _scope(store, ["docA2"])
    res = asyncio.run(tools.aggregate_ops_fields(
        store=store, field="energy charge fees", op="sum",
        role="admin", doc_ids=["docA2"], ops_scope=scope))
    # Only the base's current 0.58 — the other family's 0.99 never rides in.
    assert res["value"] == pytest.approx(0.58)
    assert res["n_docs"] == 1
    assert res["items"][0]["note"] == (
        "carried over from Base Agreement (unchanged by the pinned document)")
    assert any("carried over from Base Agreement" in (c.fact_summary or "")
               for c in res["sample"])


def test_serialized_aggregate_carries_the_marker():
    store = _FakeStore(_DOCS, _OPS)
    scope = _scope(store, ["docA2"])
    res = asyncio.run(tools.aggregate_ops_fields(
        store=store, field="energy charge fees", op="sum",
        role="admin", ops_scope=scope))
    payload = tools.serialize_tool_result("aggregate_ops_fields", res)
    assert "carried over from Base Agreement (unchanged by the pinned document)" in payload


def test_dispatch_passes_ops_scope_to_ops_tools_only(monkeypatch):
    """Raw-text scoping stays strictly pinned: the dispatch seam hands
    ops_scope to the OPS tools and to nothing else."""
    captured: dict = {}

    async def fake_ops(**kw):
        captured["ops"] = kw
        return []

    async def fake_keyword(**kw):
        captured["keyword"] = kw
        return []

    monkeypatch.setitem(agent.TOOL_DISPATCH, "lookup_verified_fields", fake_ops)
    monkeypatch.setitem(agent.TOOL_DISPATCH, "keyword_search", fake_keyword)
    scope = {"pinned": ["docA2"], "doc_ids": ["docA2", "docA"], "titles": {}}
    for name in ("lookup_verified_fields", "keyword_search"):
        asyncio.run(agent._dispatch_tool(
            name=name, args={"query": "x"}, store=None, client=None,
            embed_model="e", doc_ids=["docA2"], role="admin",
            ops_scope=scope))
    assert captured["ops"]["ops_scope"] == scope
    assert "ops_scope" not in captured["keyword"]
    assert captured["keyword"]["doc_ids"] == ["docA2"]
