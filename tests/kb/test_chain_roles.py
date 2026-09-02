"""The amendment chain reads field names from config, not from one domain.

This is the regression guard for the worst class of defect this codebase has:
a literal from the FIRST domain sitting in engine code, silently producing
nothing on any other domain. Here the literals were five field names inside
`doc_relationship`, so on a domain that spells them differently every lookup
returned None, every document tied on the undated sentinel, and "the latest
document that sets a field wins" quietly became "the last document id
alphabetically wins". No error, no log line, a plausible wrong answer.

The tests below run the walk against a domain whose field names deliberately
match nothing the engine could have hardcoded.
"""
from __future__ import annotations

import pytest
import yaml

from pipeline.kb import km, opsview_spec
from pipeline.ontology import available_doctypes


# --------------------------------------------------------------------------- #
# The seam itself
# --------------------------------------------------------------------------- #
def test_every_shipped_domain_maps_every_chain_role():
    """A role pointing at a field the view does not declare is the silent
    failure. `missing_field_roles` is what `setup --check` reports, so it has
    to be empty for everything we ship."""
    for doctype in available_doctypes():
        assert opsview_spec.missing_field_roles(doctype) == {}, (
            f"{doctype} declares a chain role reading a field it does not have")


def test_roles_resolve_to_this_domains_own_names():
    """A domain may spell chain roles differently from the engine defaults."""
    ca = opsview_spec.family_policy("commercial_agreement").field_roles
    assert ca["document_date"] == "agreement_date"
    assert ca["ordinal"] == "amendment_ordinal"
    assert ca["relationship_type"] == "document_type"


def test_unknown_role_name_is_rejected(tmp_path, monkeypatch):
    """A typo in a role name must fail loudly. Accepting it would leave the
    real role on its default, which is the silent case all over again."""
    view = {"document_family": {"field_roles": {"documnet_date": "x"}},
            "categories": {}}
    p = tmp_path / "typo_ops.yaml"
    p.write_text(yaml.safe_dump(view), encoding="utf-8")
    monkeypatch.setattr(opsview_spec, "view_path", lambda d=None: p)
    opsview_spec.family_policy.cache_clear()
    with pytest.raises(opsview_spec.OpsViewError) as exc:
        opsview_spec.family_policy("typo")
    assert "documnet_date" in str(exc.value)
    opsview_spec.family_policy.cache_clear()


def test_role_pointing_at_a_missing_field_is_rejected(tmp_path, monkeypatch):
    """Proves the gate fires. A drift gate nobody has seen fail is not known
    to work."""
    view = {
        "document_family": {"field_roles": {"document_date": "nope"}},
        "categories": {"document_relationships": {"fields": {"real_date": {}}}},
    }
    p = tmp_path / "bad_ops.yaml"
    p.write_text(yaml.safe_dump(view), encoding="utf-8")
    monkeypatch.setattr(opsview_spec, "view_path", lambda d=None: p)
    opsview_spec.family_policy.cache_clear()
    with pytest.raises(opsview_spec.OpsViewError) as exc:
        opsview_spec.family_policy("bad")
    assert "nope" in str(exc.value)
    opsview_spec.family_policy.cache_clear()


# --------------------------------------------------------------------------- #
# The walk, end to end, on a domain the engine cannot have been written for
# --------------------------------------------------------------------------- #
def _extraction(doctype: str, **role_values) -> dict:
    """Build an extraction record keyed by THIS domain's field names, given
    values keyed by the engine's role names."""
    roles = opsview_spec.family_policy(doctype).field_roles
    fields = {
        f"{opsview_spec.REL_CATEGORY}.{roles[role]}": {
            "values": [{"value": val,
                        "evidence": [{"snippet": val, "page": 1,
                                      "rects": [[0, 0, 1, 1]]}]}]}
        for role, val in role_values.items() if val is not None
    }
    return {"fields": fields}


def test_chain_reads_the_shipped_domains_own_field_names():
    """The end-to-end version of the bug: an amendment that states its date
    and its relationship must produce a dated, edged record."""
    doctype = "commercial_agreement"
    ex = _extraction(doctype,
                     document_type="Amendment",
                     ordinal="Second",
                     document_date="3 April 2024",
                     effective_date="1 May 2024")
    rel = km.doc_relationship(ex, doctype)

    assert rel["document_type"] == "Amendment"
    assert rel["ordinal"] == 2
    assert rel["document_date"] == "2024-04-03"
    assert rel["effective_date"] == "2024-05-01"
    # This domain declares the relationship by what it CALLS the document, so
    # the edge comes from the type. Reading a separate relationship_type field
    # (which this domain does not have) is what used to return None here.
    assert rel["edge"] == "AMENDS"
    assert km.best_date(rel, None) == "2024-05-01"


def test_a_dated_amendment_never_falls_back_to_the_undated_sentinel():
    """The specific silent failure: with the roles unmapped the record came
    back all-None, `best_date` returned the sentinel, and `order_family` sorted
    on doc_id. Assert the sentinel is NOT reached when the document says a
    date."""
    doctype = "commercial_agreement"
    ex = _extraction(doctype, document_type="Agreement",
                     document_date="7 January 2023")
    rel = km.doc_relationship(ex, doctype)
    assert km.best_date(rel, None) != km._UNDATED
    assert km.best_date(rel, None) == "2023-01-07"


def test_order_family_orders_by_date_not_by_doc_id():
    """The consequence the whole feature rests on. `zzz` is alphabetically
    last and earliest by date, so a chain ordering on doc_id would put it
    last and hand the wrong document the final say."""
    doctype = "commercial_agreement"
    docs = []
    for doc_id, date in (("zzz", "1 January 2020"),
                         ("aaa", "1 January 2024"),
                         ("mmm", "1 January 2022")):
        rel = km.doc_relationship(
            _extraction(doctype, document_type="Amendment", document_date=date),
            doctype)
        docs.append({"doc_id": doc_id, "best_date": km.best_date(rel, None),
                     "ordinal": rel["ordinal"]})
    assert [d["doc_id"] for d in km.order_family(docs)] == ["zzz", "mmm", "aaa"]


# --------------------------------------------------------------------------- #
# The uploader's declaration is the engine's vocabulary, not the domain's
# --------------------------------------------------------------------------- #
def test_declared_relation_produces_an_edge_on_every_domain():
    """An admin declaring 'this amends that' used to be looked up in the
    DOMAIN's relationship map, whose keys are whatever word the DOCUMENTS use.
    On any domain where those two vocabularies differed, the declared edge
    silently vanished."""
    from pipeline.kb import intake
    for relation in intake.RELATIONS:
        edge = km.declared_edge(relation)
        if relation == "standalone":
            assert edge is None
        else:
            assert edge is not None, (
                f"the upload form offers {relation!r} and it maps to no edge")


def test_declared_edge_is_independent_of_the_active_domain():
    """It is human input in the engine's own words, so it must not move when
    the domain does."""
    assert km.declared_edge("amends") == "AMENDS"
    assert km.declared_edge("novates") == "NOVATES"
    assert km.declared_edge("supersedes") == "SUPERSEDES"
    assert km.declared_edge("standalone") is None
    assert km.declared_edge(None) is None
