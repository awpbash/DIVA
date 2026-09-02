"""The pack contract, held over every domain this checkout ships.

A drift gate that only ever sees one pack cannot tell a generic compiler from a
hardcoded one, so these assert the STRUCTURAL contract, parametrized over
whatever is in configs/packs/. They are what keeps the compiler honest when a
new domain arrives, and they are what a published copy carrying a single domain
still runs. The domain-specific gate lives in test_pack.py.
"""
from __future__ import annotations

import pytest

from pipeline.extraction.pack import load
from tests.domains import ALL as DOCTYPES


@pytest.mark.parametrize("doctype", DOCTYPES)
def test_every_shipped_pack_compiles(doctype):
    pack = load(doctype)
    assert pack.doctype == doctype
    assert pack.fact_labels, f"{doctype}: no fact labels compiled"


@pytest.mark.parametrize("doctype", DOCTYPES)
def test_every_shipped_pack_inherits_the_base_layer(doctype):
    pack = load(doctype)
    assert {"Fact", "EvidenceSpan"} <= pack.structural_labels
    assert {"derived", "asserted_llm", "proposed"} <= set(pack.provenance_tiers)


@pytest.mark.parametrize("doctype", DOCTYPES)
def test_every_shipped_pack_has_consistent_projections(doctype):
    """key, indexed, filterable and summary must all be subsets of properties,
    for every fact type in every pack."""
    pack = load(doctype)
    for ft in pack.fact_types.values():
        if not ft.label:
            continue
        props = pack.properties(ft.label)
        assert ft.key in props, f"{doctype}.{ft.category}: key not in properties"
        for grp in ("indexed", "filterable", "summary"):
            missing = set(getattr(ft, grp)) - props
            assert not missing, f"{doctype}.{ft.category}.{grp}: {sorted(missing)}"


@pytest.mark.parametrize("doctype", DOCTYPES)
def test_every_shipped_pack_role_joins_resolve(doctype):
    """A role_join must name a real derived edge and a real fact label."""
    pack = load(doctype)
    for ft in pack.fact_types.values():
        if not ft.role_join:
            continue
        assert ft.role_join["edge"] in pack.derived_edges
        assert ft.role_join["to"] in pack.fact_labels


@pytest.mark.parametrize("doctype", DOCTYPES)
def test_every_shipped_pack_aggregates_on_declared_hubs(doctype):
    """Aggregation pivots must be pivotable hubs, and measures must have a
    measure block. A domain declaring fewer of either must still compile."""
    pack = load(doctype)
    for p in (pack.aggregation.get("pivots") or []):
        assert pack.hubs[p["hub"]].pivotable
    for m in (pack.aggregation.get("measures") or []):
        assert any(ft.measure for ft in pack.fact_types.values()
                   if ft.label == m["label"])


# --------------------------------------------------------------------------- #
# The graph legend. Display grouping is served from the pack, not the frontend.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("doctype", DOCTYPES)
def test_every_label_the_graph_can_emit_has_a_display_group(doctype):
    """The Explore screen used to carry its own list of node labels, copied
    from whichever domain was built first. Labels missing from that list drew
    in the unclassified grey, and on the second domain that was a quarter of
    its fact types. Nothing may be unplaced.
    """
    pack = load(doctype)
    placed = set(pack.ui_group_for)
    for label in pack.fact_labels | pack.structural_labels | set(pack.hubs):
        assert label in placed, f"{doctype}: {label} has no display group"


@pytest.mark.parametrize("doctype", DOCTYPES)
def test_every_display_group_used_is_declared(doctype):
    """A group key with no legend row would colour nodes that the legend never
    explains, and could not be toggled off."""
    pack = load(doctype)
    declared = {g["key"] for g in pack.ui_groups}
    assert declared, f"{doctype}: no ui_groups declared"
    assert set(pack.ui_group_for.values()) <= declared
    for g in pack.ui_groups:
        assert g["label"] and g["hint"], f"{doctype}: {g['key']} has no label or hint"


@pytest.mark.parametrize("doctype", DOCTYPES)
def test_fact_and_hub_labels_are_grouped_from_the_pack_not_by_name(doctype):
    """The two groups that VARY per domain are filled from the pack's own fact
    types and hubs, which is what makes the legend correct for a domain nobody
    hardcoded."""
    pack = load(doctype)
    for label in pack.fact_labels:
        assert pack.ui_group_for[label] == "fact"
    for name in pack.hubs:
        assert pack.ui_group_for[name] == "identity"


@pytest.mark.parametrize("doctype", DOCTYPES)
def test_every_structural_node_type_can_be_keyed(doctype):
    """The graph API keys nodes by the pack's declared key. A node type with
    none would be dropped silently rather than drawn."""
    pack = load(doctype)
    for label, key in pack.node_keys.items():
        assert key, f"{doctype}: {label} has no key"
    assert "Document" in pack.node_keys
