"""Edge hardening of the family DAG + currency ordering (pure functions).

The chain is the product's spine: a wrong or silently dropped edge corrupts
"current value" for every field downstream. These lock the declared-input
rules: human dates outrank extracted ones, standalone beats a recital
relation, self-links and cycles can never enter the DAG, and ties are
deterministic across rebuilds.
"""
from pipeline.kb import km


# --------------------------------------------------------------------------- #
# best_date: declared document date outranks the extracted one
# --------------------------------------------------------------------------- #

def test_declared_date_beats_extracted_document_date():
    rel = {"effective_date": None, "document_date": "2020-01-01"}
    assert km.best_date(rel, None, "2022-06-30") == "2022-06-30"


def test_extracted_effective_date_still_wins():
    # A stated amendment-effective date is a more specific axis than the
    # paper's own date, declared or not.
    rel = {"effective_date": "2023-01-01", "document_date": "2020-01-01"}
    assert km.best_date(rel, None, "2022-06-30") == "2023-01-01"


def test_no_declared_date_keeps_old_ladder():
    rel = {"effective_date": None, "document_date": None}
    assert km.best_date(rel, "2021-05-05", None) == "2021-05-05"
    assert km.best_date(rel, None, None) == km._UNDATED


# --------------------------------------------------------------------------- #
# order_family: dead ties resolve deterministically
# --------------------------------------------------------------------------- #

def test_order_family_tie_breaks_on_doc_id():
    a = {"doc_id": "bbb", "best_date": "2022-01-01", "ordinal": 1}
    b = {"doc_id": "aaa", "best_date": "2022-01-01", "ordinal": 1}
    assert [d["doc_id"] for d in km.order_family([a, b])] == ["aaa", "bbb"]
    assert [d["doc_id"] for d in km.order_family([b, a])] == ["aaa", "bbb"]


# --------------------------------------------------------------------------- #
# resolve_dag_target / resolve_family: self-links, missing parents, cycles
# --------------------------------------------------------------------------- #

def _doc(doc_id, edge=None, parent=None, best_date="2022-01-01", **kw):
    return {"doc_id": doc_id, "edge": edge, "intake_parent": parent,
            "best_date": best_date, "ordinal": 0, "document_type": "Contract",
            "amends_dated": None, **kw}


def test_self_parent_never_wins():
    base = _doc("base")
    doc = _doc("amend", edge="AMENDS", parent="amend", best_date="2023-01-01")
    # falls through to the recital path: single base resolves
    assert km.resolve_dag_target(doc, [base, doc]) == "base"


def test_resolve_family_flags_self_and_missing_parent():
    base = _doc("base")
    selfy = _doc("a1", edge="AMENDS", parent="a1", best_date="2023-01-01")
    ghost = _doc("a2", edge="AMENDS", parent="deleted", best_date="2023-02-01")
    edges, unresolved, _declared, conflicts = km.resolve_family([base, selfy, ghost])
    reasons = {(c["doc_id"], c.get("reason")) for c in conflicts}
    assert ("a1", "self") in reasons
    assert ("a2", "missing") in reasons
    # both still resolve to the single base via the recital fallback
    assert ("a1", "AMENDS", "base") in edges
    assert ("a2", "AMENDS", "base") in edges


def test_cycle_edges_drop_and_surface():
    a = _doc("a", edge="AMENDS", parent="b", best_date="2023-01-01")
    b = _doc("b", edge="AMENDS", parent="a", best_date="2023-02-01")
    edges, unresolved, _declared, conflicts = km.resolve_family([a, b])
    assert edges == []
    assert unresolved == 2
    assert {c.get("reason") for c in conflicts} == {"cycle"}


def test_legit_chain_untouched_by_cycle_guard():
    base = _doc("base")
    a1 = _doc("a1", edge="AMENDS", parent="base", best_date="2023-01-01")
    a2 = _doc("a2", edge="AMENDS", parent="a1", best_date="2023-06-01")
    edges, unresolved, _declared, conflicts = km.resolve_family([base, a1, a2])
    assert set(edges) == {("a1", "AMENDS", "base"), ("a2", "AMENDS", "a1")}
    assert unresolved == 0
    assert conflicts == []
