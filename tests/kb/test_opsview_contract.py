"""The field-schema contract, held over every domain this checkout ships.

The ops-view spec is the scored extraction target, which makes it a CORE schema
contract. These assert the parts that must hold for ANY domain, parametrized
over whatever is in configs/views/: the spec compiles against its own pack,
every label it names is a real pack fact label, and every enum is closed with an
explicit "Not Stated" so abstention is representable rather than implied by a
blank. The domain-specific gate lives in test_opsview.py.
"""
from __future__ import annotations

import pytest

from pipeline.extraction.pack import load as load_pack
from pipeline.kb.opsview_spec import family_policy
from pipeline.kb.opsview_spec import load as load_view
from pipeline.kb.opsview_spec import order_categories
from tests.domains import ALL as DOCTYPES


@pytest.mark.parametrize("doctype", DOCTYPES)
def test_every_shipped_view_compiles_against_its_own_pack(doctype):
    view = load_view(doctype=doctype)
    assert view.fields, f"{doctype}: view has no fields"
    assert view.extends_pack == doctype


@pytest.mark.parametrize("doctype", DOCTYPES)
def test_every_source_label_is_a_real_pack_fact_label(doctype):
    """No mapping may point at a fact label its pack does not declare."""
    view = load_view(doctype=doctype)
    labels = load_pack(doctype).fact_labels
    for f in view.fields:
        src = f.source
        for lbl in [src.get("label"), *(src.get("labels") or [])]:
            if lbl:
                assert lbl in labels, \
                    f"{doctype}/{f.full_key}: {lbl} is not a pack fact label"


@pytest.mark.parametrize("doctype", DOCTYPES)
def test_every_enum_is_closed_and_carries_not_stated(doctype):
    """An open enum lets extraction invent a value, and an enum with no way to
    say "Not Stated" makes abstention indistinguishable from a missed field."""
    view = load_view(doctype=doctype)
    for f in view.fields:
        if f.type in ("enum", "presence_enum"):
            assert f.values, f"{doctype}/{f.full_key}: enum has no values"
            assert "Not Stated" in f.values, \
                f"{doctype}/{f.full_key}: missing 'Not Stated'"


# --------------------------------------------------------------------------- #
# Category display order. Ordering is not filtering.
# --------------------------------------------------------------------------- #
def test_order_categories_never_drops_a_category():
    """The knowledge view and the review heatmap group fields by category and
    then order them. Both used to order against a hardcoded list of ONE
    domain's category names, which silently hid every category outside it: on
    the second domain that was 16 of 21 fields, gone from both screens with no
    error anywhere.
    """
    declared = list(load_view().categories)
    assert declared, "the active view declares no categories"
    stranger = "a_category_no_shipped_schema_declares"
    ordered = order_categories([*declared, stranger])
    assert set(ordered) == {*declared, stranger}
    assert ordered[:len(declared)] == declared
    assert ordered[-1] == stranger, "an undeclared category sorts to the end"


def test_order_categories_follows_the_schema_declaration_order():
    """A domain author controls display order by ordering their own YAML."""
    declared = list(load_view().categories)
    if len(declared) < 2:
        pytest.skip("needs a view with at least two categories")
    assert order_categories(reversed(declared)) == declared
    assert order_categories(declared[1:]) == declared[1:]


# --------------------------------------------------------------------------- #
# Intake vocabulary. What an uploader may declare a document to BE is domain
# configuration, not a literal in the engine or the frontend.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("doctype", DOCTYPES)
def test_every_domain_declares_its_own_document_types(doctype):
    """The upload form offered one domain's words to every deployment, so an
    adopter got a menu of things they do not have and no way to name what they
    do. A domain names them; the fallback is generic, never another domain's."""
    from pipeline.kb.opsview_spec import DEFAULT_DOC_TYPES, family_policy

    types = family_policy(doctype).document_types
    assert types, f"{doctype}: no document types and no fallback"
    assert len(set(types)) == len(types), f"{doctype}: duplicate document types"
    assert all(isinstance(t, str) and t.strip() for t in types)
    # The fallback must stay domain-neutral: it is what an adopter who declares
    # nothing sees on their own upload form.
    assert "Schematic/Drawing" not in DEFAULT_DOC_TYPES


@pytest.mark.parametrize("doctype", DOCTYPES)
def test_the_relation_vocabulary_is_declared_not_assumed(doctype):
    """Which word declares which edge is per domain: `amendment` in one,
    `amends` in another. The supersedence walk reads it rather than knowing."""
    policy = family_policy(doctype)
    assert policy.relation_edges, f"{doctype}: declares no relation vocabulary"
    assert set(policy.relation_edges.values()) <= {"AMENDS", "SUPERSEDES", "NOVATES"}
