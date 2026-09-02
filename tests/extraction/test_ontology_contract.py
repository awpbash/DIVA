"""The ontology-to-analyzer contract, held over every domain this checkout ships.

The analyzer schema used to pin categories to a closed 17-name enum, which meant
a new domain could not name its own concepts. The enum is now an open snake_case
pattern, and this is the check that replaced it. It is the stronger of the two,
because it catches a typo'd or orphaned category in EVERY domain rather than
only vocabulary outside one fixed list.

Domain-specific assertions live in the named ontology tests.
"""
from __future__ import annotations

import pytest

from pipeline.extraction import get_analyzer
from pipeline.extraction.ontology_compile import tables
from tests.domains import ALL as DOCTYPES


@pytest.mark.parametrize("doctype", DOCTYPES)
def test_analyzer_categories_match_their_ontology(doctype):
    analyzer = get_analyzer(doctype)
    declared = set(tables(doctype).specs)
    enabled = set(analyzer.categories)
    assert declared == enabled, (
        f"{doctype}: ontology and analyzer categories disagree: "
        f"only-in-ontology={sorted(declared - enabled)}, "
        f"only-in-analyzer={sorted(enabled - declared)}"
    )
