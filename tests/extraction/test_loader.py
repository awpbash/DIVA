"""Unit tests for pipeline/extraction/loader.py (v2 shape)."""
from __future__ import annotations

import pytest

from pipeline.extraction import get_analyzer, load_all
from pipeline.extraction.loader import ConfigError, _merge_roles
from tests.domains import skip_unless

# Hand-checked assertions about the public example analyzer.
DOMAIN = "commercial_agreement"
skip_unless(DOMAIN)


# ---------------------------------------------------------------------------
# load_all returns both analyzers from configs/
# ---------------------------------------------------------------------------


def test_load_all_discovers_every_shipped_analyzer():
    defaults, analyzers = load_all(refresh=True)
    # One entry per directory under configs/analyzers/. Adding a domain must be
    # a config-only change, so this asserts discovery rather than hidden code.
    assert set(analyzers.keys()) == {
        "_universal", DOMAIN,
    }
    assert defaults.render_dpi == 300
    assert defaults.vision_prompt == "correct_and_classify"


def test_load_all_caches():
    a = load_all()
    b = load_all()
    # Returned the same identity (module-level cache).
    assert a[1] is b[1]


# ---------------------------------------------------------------------------
# _universal sanity
# ---------------------------------------------------------------------------


def test_universal_has_all_seventeen_categories():
    a = get_analyzer("_universal")
    expected = {"organization", "person", "place", "equipment",
                "money", "rate", "formula", "cost_category",
                "date", "measurement",
                "obligation", "right", "condition", "event",
                "defined_term", "reference", "schedule"}
    assert set(a.categories) == expected


def test_universal_has_no_roles():
    a = get_analyzer("_universal")
    assert a.roles_by_category == {}


# ---------------------------------------------------------------------------
# commercial_agreement narrows universal categories and adds its own roles
# ---------------------------------------------------------------------------


def test_commercial_agreement_is_a_narrow_public_example():
    a = get_analyzer(DOMAIN)
    assert "warranty" in a.categories
    assert "equipment" not in a.categories
    assert "rate" not in a.categories


def test_commercial_agreement_money_holds_absolute_amounts_only():
    """Specific currency amounts live in `money`, not a separate rate tier."""
    a = get_analyzer(DOMAIN)
    money_roles = a.roles_for("money")
    assert "license_fee" in money_roles
    assert "minimum_commitment" in money_roles
    assert "termination_fee" in money_roles
    # Per-unit rates moved to `rate`, not money.
    assert "consumption_charge_rate" not in money_roles


def test_commercial_agreement_has_no_rate_category():
    a = get_analyzer(DOMAIN)
    assert "rate" not in a.categories
    assert a.roles_for("rate") == ()


def test_commercial_agreement_warranty_is_domain_specific():
    a = get_analyzer(DOMAIN)
    assert "warranty" in a.categories


def test_commercial_agreement_declares_organization_roles():
    a = get_analyzer(DOMAIN)
    org_roles = a.roles_for("organization")
    assert "disclosing_party" in org_roles
    assert "receiving_party" in org_roles
    assert "licensor" in org_roles
    assert "licensee" in org_roles


def test_commercial_agreement_has_right_event_condition_defined_term():
    """v3 categories — schema-fit additions."""
    a = get_analyzer(DOMAIN)
    cats = set(a.categories)
    assert {"right", "condition", "event", "defined_term"} <= cats

    assert "termination_for_convenience" in a.roles_for("right")
    assert a.roles_for("event") == ()
    assert "condition_precedent" not in a.roles_for("condition")
    assert a.roles_for("defined_term") == ()


def test_roles_for_missing_category_returns_empty_tuple():
    a = get_analyzer(DOMAIN)
    # measurement is enabled but has no roles defined
    assert a.roles_for("measurement") == ()


# ---------------------------------------------------------------------------
# Unknown analyzer
# ---------------------------------------------------------------------------


def test_get_analyzer_unknown_id_raises():
    with pytest.raises(ConfigError):
        get_analyzer("does_not_exist")


# ---------------------------------------------------------------------------
# _merge_roles — pure
# ---------------------------------------------------------------------------


def test_merge_roles_child_overrides_parent_per_category():
    parent = {"money": ["a", "b"], "date": ["x"]}
    child = {"money": ["c"]}
    out = _merge_roles(parent, child)
    assert out["money"] == ("c",)
    assert out["date"] == ("x",)


def test_merge_roles_no_parent():
    out = _merge_roles(None, {"money": ["a"]})
    assert out == {"money": ("a",)}


def test_merge_roles_no_child():
    out = _merge_roles({"date": ["x"]}, None)
    assert out == {"date": ("x",)}


def test_merge_roles_empty():
    assert _merge_roles(None, None) == {}
