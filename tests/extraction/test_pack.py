"""test_pack.py — the Doctype Pack drift gate (KB rebuild, Phase A).

The pack (configs/packs/<doctype>.yaml + the inherited _base.yaml) drives both
halves of the seam: the normalise prompts (Part 1) and the graph schema +
derivations + retrieval + aggregation (Part 2). These tests enforce that the
pack is internally consistent — the same both-directions discipline the legacy
test_ontology.py applies to the old graph layer.

Positive tests: the real public example pack compiles and its projections
agree. Negative tests: deliberately-broken mappings MUST fail to compile, so
we know the gate actually bites.

This file names ONE domain on purpose: it asserts hand-checked facts about a
real pack, which is the whole value. The structural contract every pack must
satisfy is parametrized over all shipped domains in test_pack_contract.py.
"""
from __future__ import annotations

import pytest

from pipeline.extraction.pack import (
    PackError,
    compile_mapping,
    load,
    load_pack_dict,
)
from tests.domains import skip_unless

DOMAIN = "commercial_agreement"
skip_unless(DOMAIN)

PACK = load(DOMAIN)


# ---------------------------------------------------------------------------
# Positive — the real pack compiles and is self-consistent
# ---------------------------------------------------------------------------


def test_pack_compiles():
    assert PACK.doctype == DOMAIN
    assert PACK.fact_labels, "no fact labels compiled"


def test_extends_pulled_in_the_base_layer():
    # The structural layer + provenance tiers + retrieval config live in
    # _base.yaml and reach the pack only via `extends`.
    assert "Fact" in PACK.structural_labels
    assert "EvidenceSpan" in PACK.structural_labels
    assert {"derived", "asserted_llm", "proposed"} <= set(PACK.provenance_tiers)
    assert PACK.raw.get("retrieval", {}).get("generic_fact_label") == "Fact"


def test_reference_category_routes_to_reference_label():
    assert "reference" in PACK.known_categories
    assert PACK.category_to_label["reference"] == "Reference"


def test_raw_labels_route_uniquely():
    # Compilation already rejects collisions; spot-check the routing exists.
    assert PACK.raw_label_to_category.get("obligation") == "obligation"
    assert PACK.raw_label_to_category.get("money") == "money"
    assert PACK.raw_label_to_category.get("") is None      # dropped


def test_party_label_merges_two_categories():
    # organization + person both compile to :Party with one merged config.
    assert PACK.category_to_label["organization"] == "Party"
    assert PACK.category_to_label["person"] == "Party"
    assert PACK.id_key("Party") == "party_id"


def test_retrieval_projections_subset_properties():
    # Every filterable / summary / indexed prop, and the id key, must be a
    # declared property of its label — or a retrieval query would reference a
    # field that doesn't exist on the node.
    for lab in PACK.fact_labels:
        props = PACK.properties(lab)
        assert set(PACK.filterable(lab)) <= props, lab
        assert set(PACK.summary_keys(lab)) <= props, lab
        assert set(PACK.indexed(lab)) <= props, lab
        assert PACK.id_key(lab) in props, lab


def test_derived_edge_endpoints_are_known_labels():
    known = set(PACK.fact_labels) | set(PACK.structural_labels)
    assert PACK.derived_edges, "no derived edges compiled"
    for e in PACK.derived_edges.values():
        for lab in (*e.from_labels, *e.to_labels):
            assert lab in known, f"{e.name}: {lab}"
        assert e.provenance in PACK.provenance_tiers, e.name


def test_role_join_edges_exist():
    # IMPOSED_ON / HELD_BY are referenced by obligation/right role_joins and
    # must be declared derived edges.
    assert "IMPOSED_ON" in PACK.derived_edges
    assert "HELD_BY" in PACK.derived_edges


def test_role_join_edges_carry_their_on_field():
    # Guards the YAML 1.1 'on:' boolean-key footgun: `on:` parses as the
    # boolean key True, so derive.get('on') returns None and the role-join
    # generates `x.None` Cypher that silently creates zero edges. The pack
    # uses `on_field:` to dodge it — this test fails if anyone reverts.
    for name in ("IMPOSED_ON", "HELD_BY"):
        e = PACK.derived_edges[name]
        assert e.method == "role_join", name
        assert e.on, f"{name}.on is empty — role-join would silently no-op"


def test_assertable_menu_is_fact_to_fact():
    assert {"LIMITED_BY", "EXCEPTION_TO"} <= set(PACK.assertable)
    for name, a in PACK.assertable.items():
        assert set(a.from_labels) <= PACK.fact_labels, name
        assert set(a.to_labels) <= PACK.fact_labels, name
        assert a.llm_description.strip(), name
        assert a.insert.get("asserted_llm", {}).get("min_confidence") is not None


def test_pivots_reference_pivotable_hubs():
    pivots = PACK.pivots()
    assert pivots, "no aggregation pivots declared"
    for p in pivots:
        h = p["hub"]
        assert h in PACK.hubs, h
        assert PACK.hubs[h].pivotable, h


def test_measures_resolve_to_measure_blocks():
    measures = PACK.measures()
    assert measures, "no aggregation measures declared"
    for m in measures:
        lab, pf = m["label"], m["parameter_field"]
        assert lab in PACK.fact_labels, lab
        assert pf in PACK.properties(lab), f"{lab}.{pf}"
        # the label must actually carry a measure block
        assert any(ft.measure for ft in PACK.fact_types.values() if ft.label == lab), lab


def test_quantitative_facts_carry_the_aggregation_contract():
    # The fields the cross-doc planner depends on (§ aggregation question):
    # value/parameter/bound must exist on every aggregatable label.
    for m in PACK.measures():
        lab = m["label"]
        ft = next(ft for ft in PACK.fact_types.values()
                  if ft.label == lab and ft.measure)
        for fld in ("value_field", "parameter_field", "bound_field"):
            assert ft.measure.get(fld) in PACK.properties(lab), f"{lab}.{fld}"


# ---------------------------------------------------------------------------
# Negative — the gate must reject inconsistent packs
# ---------------------------------------------------------------------------


def test_semantic_unit_tier_removed():
    # The LLM-narrated SemanticUnit tier was dropped; the recall net is the
    # deterministic same_section tier.
    assert "SemanticUnit" not in PACK.structural_labels
    for name in ("CONDITIONED_ON", "TRIGGERED_BY"):
        tiers = PACK.derived_edges[name].tiers
        assert "same_unit" not in tiers, name
        assert "same_section" in tiers, name


def test_gate_rejects_unknown_co_location_tier():
    # A removed/typo'd tier (e.g. the old same_unit) must fail the gate, not
    # silently no-op in the derive engine.
    doc = load_pack_dict(DOMAIN)
    doc["edges"]["derived"]["CONDITIONED_ON"]["derive"]["tiers"] = ["same_block", "same_unit"]
    with pytest.raises(PackError):
        compile_mapping(DOMAIN, doc)


def test_gate_rejects_unknown_filterable_prop():
    doc = load_pack_dict(DOMAIN)
    doc["fact_types"]["obligation"]["filterable"].append("not_a_real_prop")
    with pytest.raises(PackError):
        compile_mapping(DOMAIN, doc)


def test_gate_rejects_non_pivotable_pivot():
    doc = load_pack_dict(DOMAIN)
    # CanonicalTerm exists but is pivotable: false.
    doc["aggregation"]["pivots"].append({"hub": "CanonicalTerm"})
    with pytest.raises(PackError):
        compile_mapping(DOMAIN, doc)


def test_gate_rejects_unknown_edge_endpoint():
    doc = load_pack_dict(DOMAIN)
    doc["edges"]["derived"]["CONDITIONED_ON"]["to"] = ["Nonsense"]
    with pytest.raises(PackError):
        compile_mapping(DOMAIN, doc)


def test_gate_rejects_measure_with_undeclared_field():
    doc = load_pack_dict(DOMAIN)
    doc["fact_types"]["money"]["measure"]["bound_field"] = "ghost_field"
    with pytest.raises(PackError):
        compile_mapping(DOMAIN, doc)


def test_gate_rejects_duplicate_raw_label_routing():
    doc = load_pack_dict(DOMAIN)
    # Steal a raw_label that already routes to `obligation`.
    doc["fact_types"]["right"]["raw_labels"].append("obligation")
    with pytest.raises(PackError):
        compile_mapping(DOMAIN, doc)


def test_gate_rejects_a_node_type_with_no_display_group():
    # A new structural node type must declare where the legend puts it. Without
    # the gate it would just render in the unclassified grey, which is how the
    # hardcoded frontend list hid labels for so long.
    doc = load_pack_dict(DOMAIN)
    doc["structural"]["node_types"]["Document"].pop("ui_group")
    with pytest.raises(PackError):
        compile_mapping(DOMAIN, doc)


def test_gate_rejects_a_display_group_that_is_not_declared():
    doc = load_pack_dict(DOMAIN)
    doc["structural"]["node_types"]["Document"]["ui_group"] = "nonsense"
    with pytest.raises(PackError):
        compile_mapping(DOMAIN, doc)
