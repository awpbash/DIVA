"""test_ontology_compile.py — the extraction contract gate (R3-D1).

The ontology YAML's `extraction:` section drives the routing
table, the per-category normalise prompts, and the loader's category->label
mapping. These tests enforce that the YAML, the analyzer configs, the
harvest prompt's label list, and the compiled objects all agree — the same
both-directions discipline test_ontology.py applies to the graph layer.
"""
from __future__ import annotations

import re
from pathlib import Path


from pipeline import ontology
from pipeline.extraction import get_analyzer
from pipeline.extraction.ontology_compile import (
    CATEGORY_TO_LABEL,
    KNOWN_CATEGORIES,
    SPECS,
)

from tests.domains import requires_active, skip_unless

# Named explicitly: these are hand-checked assertions about the public ontology.
# The contract every domain must satisfy is in test_ontology_contract.py.
DOMAIN = "commercial_agreement"

skip_unless(DOMAIN)

# `SPECS`, `KNOWN_CATEGORIES` and `CATEGORY_TO_LABEL` are compiled from the
# ACTIVE domain, not from DOMAIN, so the tests that read them are asserting
# whichever domain is switched on. Present is not the same as active: without
# `requires_active` they fail on a checkout that ships this domain but runs
# another, which is the wrong question rather than a real regression.

ONT = ontology.load_ontology(DOMAIN)
REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Categories: YAML <-> analyzer <-> compiled objects
# ---------------------------------------------------------------------------


@requires_active(DOMAIN)
def test_specs_cover_exactly_the_analyzer_categories():
    analyzer = get_analyzer(DOMAIN)
    assert set(SPECS) == set(analyzer.categories), (
        "ontology extraction categories and analyzer categories disagree: "
        f"only-in-ontology={sorted(set(SPECS) - set(analyzer.categories))}, "
        f"only-in-analyzer={sorted(set(analyzer.categories) - set(SPECS))}"
    )


def test_spec_strings_nonempty():
    for cat, spec in SPECS.items():
        assert spec.shape_doc.strip(), f"{cat}: empty shape_doc"
        assert spec.bundling_rule.strip(), f"{cat}: empty bundling_rule"


@requires_active(DOMAIN)
def test_known_categories_include_edge_only():
    # reference is a declared category in the public ontology.
    assert "reference" in KNOWN_CATEGORIES
    assert CATEGORY_TO_LABEL["reference"] == "Reference"


@requires_active(DOMAIN)
def test_category_label_map_matches_fact_layer():
    fact_labels = ontology.fact_labels(ONT)
    assert set(CATEGORY_TO_LABEL.values()) == fact_labels


def _label_tokens(filename: str) -> set[str]:
    src = (REPO / "configs" / "prompts" / filename).read_text(encoding="utf-8")
    m = re.search(r"rough label list.*?`([^`]+)`", src, re.S)
    assert m, f"{filename} no longer contains the rough-label list"
    return {tok.strip() for tok in m.group(1).split("|") if tok.strip()}


def _harvest_label_tokens() -> set[str]:
    return _label_tokens("harvest.md")




# ---------------------------------------------------------------------------
# Assertable relations (the R3-D3 menu) + identity policy
# ---------------------------------------------------------------------------


def test_assertable_relations_are_not_direct_loader_edges():
    assertable = ontology.assertable_relations(ONT)
    assert assertable == {}


def test_identity_policy_declared():
    pol = ontology.identity_policy(ONT)
    analyzer = get_analyzer(DOMAIN)
    org_roles = set(analyzer.roles_for("organization"))
    roles = set(pol.get("canonical_roles") or [])
    assert roles, "identity.canonical_roles is empty"
    assert roles <= org_roles, (
        f"canonical_roles not in the analyzer's organization roles: {roles - org_roles}"
    )
    suffixes = pol.get("legal_suffixes") or []
    assert suffixes, "identity.legal_suffixes is empty"
    assert all(s == s.lower() for s in suffixes), "legal_suffixes must be lowercase"


def test_relation_proposal_specs_are_fact_to_fact_menu():
    fact_labels = ontology.fact_labels(ONT)
    specs = ontology.relation_proposal_specs(ONT)
    assert {"LIMITED_BY", "EXCEPTION_TO"} <= set(specs)
    for rel, spec in specs.items():
        assert (spec.get("llm_description") or spec.get("description") or "").strip(), (
            f"{rel}: no description")
        src = set(spec.get("from") or [])
        dst = set(spec.get("to") or [])
        assert src and dst, f"{rel}: missing domain/range"
        assert src <= fact_labels, f"{rel}: non-fact source labels {src - fact_labels}"
        assert dst <= fact_labels, f"{rel}: non-fact target labels {dst - fact_labels}"
