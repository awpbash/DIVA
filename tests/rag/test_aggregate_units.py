"""Chat's aggregation must never add numbers that carry different units.

The defect this guards: `aggregate_ops_fields` pooled every number regardless
of unit, then labelled the total with whichever unit appeared most often. Asked
"what do the charges add up to" over a field holding both a per-kWh rate and a
per-RTh rate, it returned one confident number that means nothing, and the
model repeated it as the answer.

`km_query.aggregate` already refused this on the knowledge surface. These tests
pin the two surfaces to the same behaviour, because chat is the one people use.
"""
from __future__ import annotations

import asyncio

import pytest

from api.rag import tools


class _FakeStore:
    """Answers the two queries `aggregate_ops_fields` makes: the opsfield rows
    and the document titles. Mirrors the fake in test_ops_pinned_family."""

    def __init__(self, docs, opsfields):
        self._docs = list(docs)
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
                    out.append({"field_key": o["field_key"], "title": o.get("title")})
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


def _doc(doc_id, title, group="fam1"):
    return {"id": doc_id, "doc_id": doc_id, "kind": "document",
            "title": title, "group": group, "effective_date": "2020-01-01"}


def _ops(ops_id, doc_id, value, unit, numbers):
    return {"id": ops_id, "ops_id": ops_id, "kind": "opsfield",
            "doc_id": doc_id, "field_key": "tariff", "full_key": "cat.tariff",
            "title": "Tariff", "category": "money", "value": value,
            "snippet": f"the tariff is {value}", "page": 1,
            "numbers": list(numbers), "unit": unit,
            "trust": "machine_extracted", "verified": False,
            "disputed": False, "is_current": True, "sensitivity": "general"}


_DOCS = [_doc("docA", "Base Agreement"), _doc("docB", "First Amendment")]

_MIXED = [_ops("m1", "docA", "0.0241 per kWh", "S$/kWh", [0.0241]),
          _ops("m2", "docB", "0.0876 per RTh", "S$/RTh", [0.0876])]

_SAME = [_ops("s1", "docA", "100", "SGD", [100.0]),
         _ops("s2", "docB", "250", "SGD", [250.0])]


def _agg(rows, op="sum"):
    return asyncio.run(tools.aggregate_ops_fields(
        store=_FakeStore(_DOCS, rows), field="tariff", op=op, role="admin"))


def test_mixed_units_refuse_a_single_total():
    res = _agg(_MIXED)
    assert res["value"] is None, "a total across two units is meaningless"
    assert res["unit"] is None, "and it must not be labelled with either unit"
    assert res["note"], "the model has to be told why there is no total"
    by_unit = {b["unit"]: b for b in res["by_unit"]}
    assert set(by_unit) == {"S$/kWh", "S$/RTh"}
    assert by_unit["S$/kWh"]["value"] == pytest.approx(0.0241)
    assert by_unit["S$/RTh"]["value"] == pytest.approx(0.0876)


def test_mixed_units_never_report_the_pooled_sum():
    """The precise wrong answer the old code produced: 0.0241 + 0.0876."""
    res = _agg(_MIXED)
    assert res["value"] != pytest.approx(0.1117)


def test_one_unit_still_totals_normally():
    res = _agg(_SAME)
    assert res["value"] == pytest.approx(350.0)
    assert res["unit"] == "SGD"
    assert "by_unit" not in res


def test_count_survives_mixed_units():
    """A tally is the one operation that stays true when units disagree."""
    res = _agg(_MIXED, op="count")
    assert res["value"] == pytest.approx(2.0)


def test_the_refusal_survives_serialisation():
    """The note and the per-unit rows are what the model actually reads, so
    they have to reach the prompt payload."""
    payload = tools.serialize_tool_result("aggregate_ops_fields", _agg(_MIXED))
    assert "multiple units" in payload
    assert "S$/kWh" in payload and "S$/RTh" in payload
