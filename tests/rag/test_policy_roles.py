"""Access-policy role vocabulary (account roles first-class) + the raw-text
source filters that close the block/section leak. Pure — YAML + string logic."""
from __future__ import annotations

import pytest

from api.rag import policy


@pytest.fixture(autouse=True)
def _fresh_policy():
    """Each test reads the YAML seed, not a mutated in-memory policy."""
    policy._ACTIVE = None
    policy._INDEX = None
    yield
    policy._ACTIVE = None
    policy._INDEX = None


# --------------------------------------------------------------------------- #
# Account-role vocabulary in the policy YAML
# --------------------------------------------------------------------------- #
def test_account_roles_are_first_class():
    assert policy.denied_classes("default") == {"financial"}
    assert policy.denied_classes("confidential") == set()
    assert policy.denied_classes("admin") == set()


def test_legacy_aliases_still_work():
    assert policy.denied_classes("general") == {"financial"}
    assert policy.denied_classes("finance") == set()


def test_unknown_role_fails_closed():
    # An unknown or missing role gets the DEFAULT (denying) role's rules.
    assert policy.denied_classes("intern") == {"financial"}
    assert policy.denied_classes(None) == {"financial"}
    assert policy.default_role() == "default"


def test_restricted_labels_for_default():
    """The invariant is domain-agnostic: an uncleared role is denied something,
    a cleared one is denied nothing. Naming one domain's money labels here is
    what let the seed drift until it matched no label at all on the domain the
    repository ships, which silently turned redaction off."""
    labs = policy.restricted_labels("default")
    assert labs, "the default role must be denied something, or redaction is off"
    assert policy.restricted_labels("admin") == set()


def test_every_restricted_label_exists_in_the_active_pack():
    """The check that would have caught the inert seed. A class naming a label
    the pack does not declare matches no fact, so the redaction never fires and
    nothing anywhere reports it."""
    assert policy.unmatched_labels() == {}


# --------------------------------------------------------------------------- #
# Raw-text source filters (the leak fix): blocks/sections filter per role
# --------------------------------------------------------------------------- #
def test_text_clearance_roles():
    from api.rag.tools import _text_cleared
    for role in ("admin", "confidential", "finance", "ADMIN"):
        assert _text_cleared(role) is True
    for role in ("default", "general", "", None):
        assert _text_cleared(role) is False


def test_block_guard_is_a_field_check():
    # Cosmos port: the per-query Cypher guard became a plain field check at
    # enrichment time — losing it silently reopens the leak, so pin the
    # truth table.
    from api.rag.tools import _block_visible
    conf = {"sensitivity": "confidential"}
    assert _block_visible(conf, "admin") is True
    assert _block_visible(conf, "confidential") is True
    assert _block_visible(conf, "default") is False
    assert _block_visible(conf, None) is False
    assert _block_visible({"sensitivity": "general"}, "default") is True
    assert _block_visible({}, "default") is True     # untagged = general


def test_span_and_mention_guard_is_a_field_check():
    # The cross-layer net: a span/mention citing a confidential block (incl.
    # blocks tagged by VALUE propagation) carries ``cites_confidential``
    # (stamped at KM build) and is invisible to uncleared roles across
    # keyword, vector-evidence AND orphan-mention recall.
    from api.rag.tools import _span_visible
    tagged = {"cites_confidential": True}
    assert _span_visible(tagged, "admin") is True
    assert _span_visible(tagged, "confidential") is True
    assert _span_visible(tagged, "default") is False
    assert _span_visible(tagged, None) is False
    assert _span_visible({}, "default") is True      # clean span: visible to all


def test_value_token_net_catches_mislabelled_citations():
    # Labels alone are a loophole: a Formula-labelled fact carrying a rate, or a
    # visible citation whose row_context includes the adjacent table cell, must
    # still be restricted for an uncleared role once the doc's value tokens say so.
    from api.rag.schemas import Citation
    formula = Citation(evidence_id="e1", doc_id="d1", page_no=3,
                       snippet="the charge shall be S$0.2485 per RTh by formula",
                       fact_label="Formula")
    row_smuggle = Citation(evidence_id="e2", doc_id="d1", page_no=4,
                           snippet="24 months",
                           row_context="Term | 24 months | S$0.2485/RTh",
                           fact_label="Duration")
    clean = Citation(evidence_id="e3", doc_id="d1", page_no=5,
                     snippet="maintain confidential information", fact_label="Obligation")
    other_doc = Citation(evidence_id="e4", doc_id="d2", page_no=1,
                         snippet="the charge shall be S$0.2485 per RTh",
                         fact_label="Formula")   # d2 has no tokens → label rules alone
    cs = [formula, row_smuggle, clean, other_doc]
    policy.tag_citations(cs, "default", doc_tokens={"d1": ["0.2485"]})
    assert formula.restricted and formula.sensitivity == "financial"
    assert row_smuggle.restricted
    assert not clean.restricted
    assert not other_doc.restricted            # tokens are per-document
    visible, hidden = policy.partition(cs, "default")
    assert {c.evidence_id for c in hidden} == {"e1", "e2"}
    # Cleared roles: the net never fires.
    policy.tag_citations(cs, "admin", doc_tokens={"d1": ["0.2485"]})
    assert not formula.restricted and not row_smuggle.restricted


def test_distinctive_value_tokens():
    # Value propagation tags blocks by CONFIDENTIAL VALUE tokens — they must be
    # precise (decimals, grouped amounts, long digit runs, emails), never bare
    # small integers that would blanket half the document.
    from pipeline.kb.km import distinctive_value_tokens as toks
    assert toks("S$ 0.2485 / RTh") == ["0.2485"]
    assert toks("cap of S$550,000 aggregate") == ["550,000"]
    assert toks("tel: 91234567") == ["91234567"]
    assert toks("ops.lead@example.com") == ["ops.lead@example.com"]
    assert toks("12 months from 2020") == []          # bare ints/years: too broad
    assert toks("7.0 ± 1 °C") == []                   # short decimal: too broad
    assert toks("Implicit") == []
