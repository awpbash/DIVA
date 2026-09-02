"""Drift gate for the public ops-view spec.

This is a CORE schema contract — the scored extraction target — so it gets the
formal gate: the real spec must compile against the real pack (no mapping can
reference a non-existent fact label / property, no enum may be left open), and
the compiler must REJECT the obvious ways to break it. Pure, no network.

This file names ONE domain on purpose: its assertions are hand-checked facts
about a real field schema. The contract every schema must satisfy, parametrized
over all shipped domains, is in test_opsview_contract.py.
"""
from __future__ import annotations

import pytest

from pipeline.extraction.pack import load as load_pack
from pipeline.kb.opsview_spec import (
    OpsViewError, compile_view, load as load_view,
)
from tests.domains import skip_unless

# Named explicitly so this gate holds its domain even when another one is
# active, which is what makes it a drift gate rather than a mood ring.
DOMAIN = "commercial_agreement"

skip_unless(DOMAIN)

EXPECTED_CATEGORIES = {
    "admin", "commercial_terms", "document_relationships", "governing_law",
    "obligations", "parties", "rights", "term",
}


def test_real_view_compiles_against_real_pack():
    """The shipped spec is consistent with the public pack."""
    view = load_view(doctype=DOMAIN)
    assert view.fields, "view has no fields"
    assert set(view.by_category()) == EXPECTED_CATEGORIES


def test_every_source_label_is_a_real_pack_fact_label():
    """No mapping may point at a fact label the pack doesn't declare."""
    view = load_view(doctype=DOMAIN)
    labels = load_pack(DOMAIN).fact_labels
    for f in view.fields:
        src = f.source
        for lbl in [src.get("label"), *(src.get("labels") or [])]:
            if lbl:
                assert lbl in labels, f"{f.full_key}: {lbl} not a pack fact label"


def test_all_enums_are_closed_and_include_not_stated():
    view = load_view(doctype=DOMAIN)
    for f in view.fields:
        if f.type in ("enum", "presence_enum"):
            assert f.values, f"{f.full_key}: enum has no values"
            assert "Not Stated" in f.values, f"{f.full_key}: enum missing 'Not Stated'"


def test_enum_role_maps_only_emit_declared_values():
    """A responsibility mapping can't produce a value outside the closed enum."""
    view = load_view(doctype=DOMAIN)
    for f in view.fields:
        if f.mechanism == "enum":
            for v in (f.source.get("role_map") or {}).values():
                assert v in f.values, f"{f.full_key}: role_map emits {v!r} not in {f.values}"


# --------------------------------------------------------------------------- #
# The gate must REJECT drift, not just accept the good spec.
# --------------------------------------------------------------------------- #

def _doc(field: dict) -> dict:
    return {
        "version": 0, "extends_pack": DOMAIN,
        "enums": {"responsibility": ["Licensor", "Licensee", "Both", "Not Stated"]},
        "categories": {"x": {"title": "X", "fields": {"f": field}}},
    }


def test_gate_rejects_unknown_fact_label():
    pack = load_pack(DOMAIN)
    bad = _doc({"title": "F", "type": "value", "multiplicity": 1,
                "source": {"mechanism": "value", "label": "Nonsense",
                           "match": {"parameter_any": ["z"]}}})
    with pytest.raises(OpsViewError):
        compile_view(bad, pack)


def test_gate_rejects_open_enum_without_not_stated():
    pack = load_pack(DOMAIN)
    bad = _doc({"title": "F", "type": "enum", "multiplicity": 1,
                "values": ["Supplier", "Customer"],   # no 'Not Stated'
                "source": {"mechanism": "enum", "label": "Obligation",
                           "match": {"action_prefix": ["maintain"]},
                           "role_map": {"licensor": "Licensor"}}})
    with pytest.raises(OpsViewError):
        compile_view(bad, pack)


def test_gate_rejects_enum_on_label_without_canonical_action():
    pack = load_pack(DOMAIN)
    bad = _doc({"title": "F", "type": "enum", "multiplicity": 1,
                "values_ref": "responsibility",
                "source": {"mechanism": "enum", "label": "Measurement",  # no canonical_action
                           "match": {"action_prefix": ["maintain"]},
                           "role_map": {"licensor": "Licensor"}}})
    with pytest.raises(OpsViewError):
        compile_view(bad, pack)


def test_gate_rejects_unknown_mechanism():
    pack = load_pack(DOMAIN)
    bad = _doc({"title": "F", "type": "text", "multiplicity": 1,
                "source": {"mechanism": "telepathy"}})
    with pytest.raises(OpsViewError):
        compile_view(bad, pack)


def test_gate_rejects_party_with_unknown_role():
    """A `party` field must name a real party role, not a typo that matches nothing."""
    pack = load_pack(DOMAIN)
    bad = _doc({"title": "F", "type": "text", "multiplicity": 1,
                "source": {"mechanism": "party", "role": "licnesor"}})
    with pytest.raises(OpsViewError):
        compile_view(bad, pack)


def test_party_fields_present_and_general():
    """Signatory is visible to everyone; party identity stays confidential."""
    view = load_view(doctype=DOMAIN)
    keys = {f.full_key for f in view.fields}
    assert "parties.signatory_name" in keys
    for f in view.fields:
        if f.full_key == "parties.signatory_name":
            assert f.sensitivity == "general", f"{f.full_key} should be general-visibility"
