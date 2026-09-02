"""A correction must move the current value immediately, not at the next build.

`states_a_value` treats "Not Stated" as the absence of a statement, so a
verifier correcting a misread to "Not Stated" hands the current value back to
an earlier document. The vote path used to stamp only the node it edited, which
left the corrected document still marked current and still marked as having
superseded the value below it. The correction looked like it did nothing.
"""
from __future__ import annotations

import pytest

from pipeline.kb import km


class _FakeStore:
    """Enough Cosmos to run the refresh: two query shapes and a patch."""

    def __init__(self, docs: list[dict], fields: list[dict]):
        self.docs = docs
        self.fields = {(f["doc_id"], f["full_key"]): f for f in fields}
        self.patches: list[tuple[str, str, list[dict]]] = []

    def query(self, sql: str, params: list[dict] | None = None) -> list[dict]:
        p = {x["name"]: x["value"] for x in (params or [])}
        if "kind = 'document'" in sql:
            return [dict(d) for d in self.docs]
        if "kind = 'opsfield'" in sql:
            return [dict(f) for f in self.fields.values()
                    if f["full_key"] == p["@fk"] and f["doc_id"] in p["@ids"]]
        raise AssertionError(f"unexpected query: {sql}")

    def patch(self, item_id: str, pk: str, ops: list[dict]) -> None:
        self.patches.append((item_id, pk, ops))
        for op in ops:
            self.fields[(pk, item_id.split(":", 1)[1])][op["path"].lstrip("/")] = op["value"]

    def applied(self) -> dict[str, tuple]:
        return {doc: (f["is_current"], f["superseded_by"])
                for (doc, _), f in self.fields.items()}


FK = "commercial_terms.audit_right"


def _docs(*rows):
    return [{"doc_id": d, "grp": "fam", "chain_date": date, "chain_ordinal": 0}
            for d, date in rows]


def _field(doc_id, values, *, is_current=True, superseded_by=None,
           category="commercial_terms", multiplicity=1):
    return {"doc_id": doc_id, "full_key": FK, "category": category,
            "vals": values, "multiplicity": multiplicity,
            "values": values, "is_current": is_current,
            "superseded_by": superseded_by}


def test_correcting_to_not_stated_hands_the_value_back():
    """The bug. The amendment's misread superseded the base; correcting it to
    "Not Stated" must make the base current again in the same request."""
    store = _FakeStore(
        _docs(("base", "2020-01-01"), ("amend", "2023-01-01")),
        [_field("base", ["Yes"], is_current=False, superseded_by="amend"),
         _field("amend", ["Not Stated"])])          # just corrected

    assert km.refresh_field_currency(store, "amend", FK) == 2
    assert store.applied() == {"base": (True, None), "amend": (False, None)}


def test_correcting_into_a_value_takes_the_current_slot():
    """The other direction: a document that stated nothing now does, and it is
    the newest, so it must supersede the base."""
    store = _FakeStore(
        _docs(("base", "2020-01-01"), ("amend", "2023-01-01")),
        [_field("base", ["Yes"]),
         _field("amend", ["No"], is_current=False)])

    assert km.refresh_field_currency(store, "amend", FK) == 2
    assert store.applied() == {"base": (False, "amend"), "amend": (True, None)}


def test_an_unchanged_family_is_not_rewritten():
    """No patch when nothing moved: a vote that only endorses the machine value
    must not churn the store."""
    store = _FakeStore(
        _docs(("base", "2020-01-01"), ("amend", "2023-01-01")),
        [_field("base", ["Yes"], is_current=False, superseded_by="amend"),
         _field("amend", ["No"])])

    assert km.refresh_field_currency(store, "amend", FK) == 0
    assert store.patches == []


def test_a_family_of_one_is_left_alone():
    store = _FakeStore(_docs(("solo", "2020-01-01")), [_field("solo", ["Yes"])])
    assert km.refresh_field_currency(store, "solo", FK) == 0


def test_a_store_without_chain_dates_is_left_alone():
    """Written before chain_date existed. Treating every document as undated
    would mark every statement current, which is worse than waiting for the
    next build."""
    docs = [{"doc_id": "base", "grp": "fam"}, {"doc_id": "amend", "grp": "fam"}]
    store = _FakeStore(docs, [_field("base", ["Yes"], is_current=False,
                                     superseded_by="amend"),
                              _field("amend", ["Not Stated"])])
    assert km.refresh_field_currency(store, "amend", FK) == 0
    assert store.patches == []


def test_identity_categories_are_left_to_the_full_build():
    """Party currency depends on the novation carve-out, which needs document
    records this path does not have. Abstain rather than guess."""
    cat = sorted(km._non_superseding())[0]
    store = _FakeStore(
        _docs(("base", "2020-01-01"), ("amend", "2023-01-01")),
        [_field("base", ["A Ltd"], category=cat),
         _field("amend", ["Not Stated"], category=cat)])
    assert km.refresh_field_currency(store, "amend", FK) == 0


def test_multi_valued_fields_all_stay_current():
    """A list-valued field accumulates. Superseding one would drop everything
    the earlier documents listed."""
    store = _FakeStore(
        _docs(("base", "2020-01-01"), ("amend", "2023-01-01")),
        [_field("base", ["A"], is_current=False, superseded_by="amend",
                multiplicity=9),
         _field("amend", ["B"], multiplicity=9)])
    km.refresh_field_currency(store, "amend", FK)
    assert store.applied() == {"base": (True, None), "amend": (True, None)}


def test_an_undated_member_never_holds_the_current_value():
    """No resolvable date means no position in the chain, so it cannot be the
    document a reader is sent to."""
    store = _FakeStore(
        _docs(("base", "2020-01-01")) + [
            {"doc_id": "loose", "grp": "fam", "chain_date": km._UNDATED,
             "chain_ordinal": 0}],
        [_field("base", ["Yes"]), _field("loose", ["No"])])
    km.refresh_field_currency(store, "loose", FK)
    assert store.applied() == {"base": (True, None), "loose": (False, None)}


def test_an_unknown_document_is_a_no_op():
    store = _FakeStore(_docs(("base", "2020-01-01")), [_field("base", ["Yes"])])
    assert km.refresh_field_currency(store, "ghost", FK) == 0


@pytest.mark.parametrize("values", [[], [""], ["not stated"], ["NOT STATED"]])
def test_every_spelling_of_absence_counts_as_not_stated(values):
    store = _FakeStore(
        _docs(("base", "2020-01-01"), ("amend", "2023-01-01")),
        [_field("base", ["Yes"], is_current=False, superseded_by="amend"),
         _field("amend", values)])
    km.refresh_field_currency(store, "amend", FK)
    assert store.applied()["base"] == (True, None)
