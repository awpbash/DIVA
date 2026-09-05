"""Unit tests for the KM layer's PURE logic (pipeline/kb/km.py).

Number/date parsing, resolve-and-link canonical keys, family ordering, the per-field
supersedence walk, and DAG target resolution — all exercised without a graph, so this
is free + deterministic and gates the load-time derivation the aligned KB stands on.
"""
from __future__ import annotations

from pipeline.kb import km
from tests.domains import requires_active


# --------------------------------------------------------------------------- #
# Number parsing — the aggregation primitive.
# --------------------------------------------------------------------------- #
def test_parse_numbers_currency_and_thousands():
    assert km.parse_numbers("S$1,576,035") == [1576035.0]
    assert km.parse_numbers("S$159,455.60 payable by Customer") == [159455.60]
    assert km.parse_numbers("S$0.0241/RTh") == [0.0241]


def test_parse_numbers_decimal_comma_vs_thousands():
    # a 3-digit group is thousands; a non-3-digit group is a decimal comma (e.g. barg).
    assert km.parse_numbers("1,000 RT") == [1000.0]
    assert km.parse_numbers("4,5 barg") == [4.5]


def test_parse_numbers_multiple_and_empty():
    assert km.parse_numbers("Cooling 0.0241; Chilled 0.0876") == [0.0241, 0.0876]
    assert km.parse_numbers("Not Stated") == []
    assert km.parse_numbers(None) == []


def test_parse_unit_best_effort():
    assert km.parse_unit("7 °C ± 1 °C").lower().startswith("°c") or "c" in km.parse_unit("7 °C").lower()
    assert km.parse_unit("no unit here") is None


# --------------------------------------------------------------------------- #
# Resolve-and-link — suffix-invariant party key.
# --------------------------------------------------------------------------- #
def test_canonical_party_key_collapses_suffix_and_case():
    a = km.canonical_party_key("ALDER GROVE UTILITIES PTE. LTD.")
    b = km.canonical_party_key("Alder Grove Utilities Pte Ltd")
    c = km.canonical_party_key("alder grove utilities")
    assert a == b == c == "alder grove utilities"


def test_canonical_party_key_abstains_when_too_short():
    assert km.canonical_party_key("") == ""
    assert km.canonical_party_key("X") == ""        # too short to trust → abstain, don't mint


def test_canonical_party_key_skips_template_tokens():
    # A pro-forma / tech-spec names its parties generically — never a real entity hub.
    assert km.canonical_party_key("VENDOR") == ""
    assert km.canonical_party_key("The Company") == ""
    assert km.canonical_party_key("Customer") == ""
    # A real entity still resolves.
    assert km.canonical_party_key("Northwind Logistics") == "northwind logistics"


def test_canonical_party_key_strips_slashes():
    # This key becomes a Cosmos item id (CanonicalParty:<key>), and Cosmos
    # rejects '/' in an id with a 400. A real entity's former name, "f/k/a"
    # or "d/b/a", broke every later knowledge-graph build until this held.
    key = km.canonical_party_key("Glu Mobile Inc. f/k/a Sorrent, Inc")
    assert "/" not in key
    assert key == "glu mobile inc f k a sorrent"


# --------------------------------------------------------------------------- #
# Recital-field parsing → relationship + ordinal.
# --------------------------------------------------------------------------- #
def test_ordinal_int():
    assert km.ordinal_int("Third") == 3
    assert km.ordinal_int("First Supplemental") == 1
    assert km.ordinal_int("None") == 0
    assert km.ordinal_int("Standalone") == 0
    assert km.ordinal_int(None) == 0


@requires_active("commercial_agreement")
def test_rel_edge():
    assert km.rel_edge("Amendment") == "AMENDS"
    assert km.rel_edge("Assignment") == "NOVATES"
    assert km.rel_edge("Amended and Restated") == "SUPERSEDES"
    assert km.rel_edge("Agreement") is None
    assert km.rel_edge("Not Stated") is None


def _extraction(fields: dict) -> dict:
    """Wrap {full_key: value} into the field_llm cache shape."""
    return {"fields": {fk: {"values": [{"value": v, "evidence":
            [{"snippet": v, "page": 1, "rects": "[]"}]}]} for fk, v in fields.items()}}


@requires_active("commercial_agreement")
def test_doc_relationship_parses_recital_fields():
    ex = _extraction({
        "document_relationships.document_type": "Amendment",
        "document_relationships.amendment_ordinal": "Third",
        "document_relationships.agreement_date": "17 October 2022",
        "document_relationships.amends_document": "3 April 2020",
        "document_relationships.effective_date": "01 October 2022",
    })
    rel = km.doc_relationship(ex)
    assert rel["edge"] == "AMENDS"
    assert rel["ordinal"] == 3
    assert rel["document_date"] == "2022-10-17"
    assert rel["amends_dated"] == "2020-04-03"
    assert rel["effective_date"] == "2022-10-01"


def test_field_values_extracts_value_and_evidence():
    ex = _extraction({"commercial_link.demand_charge_fees": "S$159,455.60"})
    vals = km.field_values(ex, "commercial_link.demand_charge_fees")
    assert vals and vals[0]["value"] == "S$159,455.60"
    assert vals[0]["has_evidence"] is False       # rects "[]" is empty → not backed
    assert km.field_values(ex, "nonexistent.field") == []


def test_field_values_counts_real_rects_as_backed():
    ex = {"fields": {"sla.rate": {"values": [{"value": "S$5", "evidence":
          [{"snippet": "S$5", "page": 2, "rects": '[{"page_no": 2, "bbox": [0,0,1,1]}]'}]}]}}}
    vals = km.field_values(ex, "sla.rate")
    assert vals[0]["has_evidence"] is True
    assert vals[0]["page"] == 2


def test_field_values_respects_reviewer_rejections():
    ex = {"fields": {"sla.rate": {"values": [
        {"value": "7 °C", "evidence": [
            {"snippet": "the rate is S$0.0241/RTh", "page": 3,
             "rects": '[{"page_no": 3, "bbox": [0,0,1,1]}]'},                 # wrong clause
            {"snippet": "supply temperature of 7 °C", "page": 2,
             "rects": '[{"page_no": 2, "bbox": [0,0,1,1]}]'},                 # right clause
        ]},
    ]}}}
    rejected = frozenset([km.ev_key(3, "the rate is S$0.0241/RTh")])
    vals = km.field_values(ex, "sla.rate", rejected=rejected)
    assert vals[0]["page"] == 2                    # highlight moved to the surviving clause
    # Rejecting EVERY citation withdraws the value entirely.
    both = frozenset([km.ev_key(3, "the rate is S$0.0241/RTh"),
                      km.ev_key(2, "supply temperature of 7 °C")])
    assert km.field_values(ex, "sla.rate", rejected=both) == []


def test_added_values_append_in_field_shape():
    ov = {"added": [{"value": "2 chiller systems", "snippet": "two (2) chiller systems",
                     "page": 1, "rects": '[{"page_no": 1, "bbox": [0,0,1,1]}]'}]}
    vals = km.added_values(ov)
    assert vals[0]["value"] == "2 chiller systems" and vals[0]["has_evidence"] is True
    assert km.added_values(None) == []


# --------------------------------------------------------------------------- #
# Family ordering + per-field supersedence walk.
# --------------------------------------------------------------------------- #
def test_order_family_by_date_then_ordinal():
    fam = [
        {"doc_id": "amend2", "best_date": "2022-09-07", "ordinal": 2},
        {"doc_id": "base", "best_date": "2020-04-03", "ordinal": 0},
        {"doc_id": "amend1", "best_date": "2021-03-12", "ordinal": 1},
    ]
    order = [d["doc_id"] for d in km.order_family(fam)]
    assert order == ["base", "amend1", "amend2"]


def test_supersedence_latest_wins_for_single_valued():
    ordered = [
        {"doc_id": "base", "stated_fields": {"sla.rate", "scope.temp"}},
        {"doc_id": "amend1", "stated_fields": {"sla.rate"}},           # re-states the rate
    ]
    mult = {"sla.rate": 1, "scope.temp": 1}
    st = km.supersedence(ordered, mult)
    # amend1's rate is current; base's rate is superseded by amend1.
    assert st[("amend1", "sla.rate")] == {"is_current": True, "superseded_by": None}
    assert st[("base", "sla.rate")] == {"is_current": False, "superseded_by": "amend1"}
    # temp is stated only by base → stays current (inherited).
    assert st[("base", "scope.temp")]["is_current"] is True


def test_supersedence_multivalued_fields_accumulate():
    ordered = [
        {"doc_id": "base", "stated_fields": {"equip.name"}},
        {"doc_id": "amend1", "stated_fields": {"equip.name"}},
    ]
    mult = {"equip.name": 100}      # multi-valued: an amendment doesn't wipe the list
    st = km.supersedence(ordered, mult)
    assert st[("base", "equip.name")]["is_current"] is True
    assert st[("amend1", "equip.name")]["is_current"] is True


# --------------------------------------------------------------------------- #
# DAG target resolution.
# --------------------------------------------------------------------------- #
def test_resolve_dag_target_prefers_date_match():
    fam = [
        {"doc_id": "base", "edge": None, "document_date": "2020-04-03"},
        {"doc_id": "base2", "edge": None, "document_date": "2019-01-01"},
        {"doc_id": "amend", "edge": "AMENDS", "amends_dated": "2020-04-03"},
    ]
    amend = fam[2]
    assert km.resolve_dag_target(amend, fam) == "base"     # date match beats ambiguity


def test_resolve_dag_target_single_base_fallback():
    fam = [
        {"doc_id": "base", "edge": None, "document_date": "2020-04-03"},
        {"doc_id": "amend", "edge": "AMENDS", "amends_dated": None},
    ]
    assert km.resolve_dag_target(fam[1], fam) == "base"


def test_resolve_dag_target_ambiguous_returns_none():
    fam = [
        {"doc_id": "base1", "edge": None, "document_date": "2020-04-03"},
        {"doc_id": "base2", "edge": None, "document_date": "2019-01-01"},
        {"doc_id": "amend", "edge": "AMENDS", "amends_dated": None},
    ]
    # two bases, no date to disambiguate → abstain (never guess a chain).
    assert km.resolve_dag_target(fam[2], fam) is None


def test_base_document_has_no_outgoing_edge():
    fam = [{"doc_id": "base", "edge": None, "document_date": "2020-04-03"}]
    assert km.resolve_dag_target(fam[0], fam) is None


@requires_active("commercial_agreement")
def test_resolve_dag_target_ignores_ancillary_base():
    # A side letter has no edge but is NOT a chain base, so the single real base resolves.
    fam = [
        {"doc_id": "base", "edge": None, "document_type": "Agreement", "document_date": None},
        {"doc_id": "side", "edge": None, "document_type": "Side Letter", "document_date": None},
        {"doc_id": "amend", "edge": "AMENDS", "document_type": "Amendment", "amends_dated": None},
    ]
    assert km.resolve_dag_target(fam[2], fam) == "base"


def test_resolve_dag_target_temporal_filter_breaks_base_tie():
    # Two "base" docs compete (one is a misfiled later record) — a 2020 amendment
    # cannot amend a 2022 document, so the future candidate drops out.
    fam = [
        {"doc_id": "real_base", "edge": None, "document_date": None, "best_date": "2019-07-26"},
        {"doc_id": "later_fdd", "edge": None, "document_date": "2022-11-01", "best_date": "2022-11-01"},
        {"doc_id": "amend", "edge": "AMENDS", "amends_dated": "2020-04-03", "best_date": "2020-04-30"},
    ]
    assert km.resolve_dag_target(fam[2], fam) == "real_base"


def test_resolve_dag_target_temporal_filter_keeps_undated_candidates():
    # An undated candidate can't be excluded on evidence — it survives the filter.
    fam = [
        {"doc_id": "old_undated", "edge": None, "document_date": None, "best_date": km._UNDATED},
        {"doc_id": "newer_base", "edge": None, "document_date": "2020-10-01", "best_date": "2020-10-01"},
        {"doc_id": "novation", "edge": "NOVATES", "amends_dated": "1999-08-02", "best_date": "2002-11-19"},
    ]
    assert km.resolve_dag_target(fam[2], fam) == "old_undated"


def test_resolve_dag_target_undated_source_still_abstains():
    # Without a date on the amending doc the temporal filter can't run — abstain.
    fam = [
        {"doc_id": "base1", "edge": None, "document_date": "2020-04-03", "best_date": "2020-04-03"},
        {"doc_id": "base2", "edge": None, "document_date": "2019-01-01", "best_date": "2019-01-01"},
        {"doc_id": "amend", "edge": "AMENDS", "amends_dated": None, "best_date": km._UNDATED},
    ]
    assert km.resolve_dag_target(fam[2], fam) is None


def test_group_families_ungrouped_docs_are_singletons():
    recs = [
        {"doc_id": "a", "group": "Family X"},
        {"doc_id": "b", "group": "Family X"},
        {"doc_id": "c", "group": None},
        {"doc_id": "d", "group": None},
    ]
    fams = km.group_families(recs)
    assert sorted(len(f) for f in fams.values()) == [1, 1, 2]
    # Ungrouped docs never share a family — so they can never supersede each other.
    assert {r["doc_id"] for r in fams["solo:c"]} == {"c"}
    assert {r["doc_id"] for r in fams["solo:d"]} == {"d"}


def test_resolve_family_backfills_declared_date_for_undated_target():
    # The base's own date extraction failed, but its novation DECLARES the date
    # ("the Agreement dated 2 August 1999") — the chain inherits that evidence.
    fam = [
        {"doc_id": "old_base", "edge": None, "document_date": None, "best_date": km._UNDATED},
        {"doc_id": "newer_base", "edge": None, "document_date": "2020-10-01", "best_date": "2020-10-01"},
        {"doc_id": "novation", "edge": "NOVATES", "amends_dated": "1999-08-02", "best_date": "2002-11-19"},
        {"doc_id": "letter", "edge": "AMENDS", "amends_dated": None, "best_date": "2000-12-20"},
    ]
    edges, unresolved, declared, conflicts = km.resolve_family(fam)
    assert ("novation", "NOVATES", "old_base") in edges
    assert ("letter", "AMENDS", "old_base") in edges
    assert unresolved == 0
    assert conflicts == []
    assert declared == {"old_base": "1999-08-02"}
    assert fam[0]["best_date"] == "1999-08-02"        # mutated in place for ordering
    # And with the backfilled date, the newer base now supersedes the old one's
    # overlapping single-valued fields in the ordered chain.
    ordered = km.order_family([
        {"doc_id": d["doc_id"], "best_date": d["best_date"], "ordinal": 0,
         "stated_fields": {"commercial_link.energy_charge_fees"}}
        for d in fam if d["best_date"] != km._UNDATED])
    status = km.supersedence(ordered, {"commercial_link.energy_charge_fees": 1})
    assert status[("old_base", "commercial_link.energy_charge_fees")]["is_current"] is False
    assert status[("newer_base", "commercial_link.energy_charge_fees")]["is_current"] is True


def test_resolve_family_never_overrides_a_dated_target():
    fam = [
        {"doc_id": "base", "edge": None, "document_date": None, "best_date": "2019-07-26"},
        {"doc_id": "amend", "edge": "AMENDS", "amends_dated": "2020-04-03", "best_date": "2020-04-30"},
    ]
    _edges, _unres, declared, _conflicts = km.resolve_family(fam)
    assert declared == {}
    assert fam[0]["best_date"] == "2019-07-26"        # its own date wins


# --------------------------------------------------------------------------- #
# Declared intake: uploader-declared parent + party currency (novation)
# --------------------------------------------------------------------------- #
def test_declared_parent_wins_and_recital_agreement_is_quiet():
    fam = [
        {"doc_id": "base", "edge": None, "document_date": "2019-01-01", "best_date": "2019-01-01"},
        {"doc_id": "nov", "edge": "NOVATES", "intake_parent": "base",
         "amends_dated": None, "best_date": "2022-06-01"},
    ]
    edges, unresolved, _declared, conflicts = km.resolve_family(fam)
    assert ("nov", "NOVATES", "base") in edges
    assert unresolved == 0
    assert conflicts == []                     # recital resolves to the same base


def test_declared_parent_conflicting_with_recital_is_flagged_not_replaced():
    # The recital's date match points at base_a; the uploader declared base_b.
    fam = [
        {"doc_id": "base_a", "edge": None, "document_date": "2019-01-01", "best_date": "2019-01-01"},
        {"doc_id": "base_b", "edge": None, "document_date": "2020-01-01", "best_date": "2020-01-01"},
        {"doc_id": "amend", "edge": "AMENDS", "intake_parent": "base_b",
         "amends_dated": "2019-01-01", "best_date": "2021-01-01"},
    ]
    edges, _unres, _declared, conflicts = km.resolve_family(fam)
    assert ("amend", "AMENDS", "base_b") in edges       # human input wins
    assert conflicts == [{"doc_id": "amend", "declared": "base_b", "recital": "base_a"}]


def test_declared_parent_resolves_an_otherwise_ambiguous_family():
    fam = [
        {"doc_id": "base_a", "edge": None, "document_date": None, "best_date": "2019-01-01"},
        {"doc_id": "base_b", "edge": None, "document_date": None, "best_date": "2019-06-01"},
        {"doc_id": "amend", "edge": "AMENDS", "intake_parent": "base_a",
         "amends_dated": None, "best_date": "2021-01-01"},
    ]
    edges, unresolved, _declared, conflicts = km.resolve_family(fam)
    assert edges == [("amend", "AMENDS", "base_a")]
    assert unresolved == 0
    assert conflicts == []                     # recital abstains (two bases) — no quarrel


@requires_active("commercial_agreement")
def test_party_supersedable_gates_placeholders_and_ancillary_docs():
    chain_doc = {"document_type": "Agreement"}
    ancillary = {"document_type": "Side Letter"}
    real = {"mechanism": "party", "values": ["Northwind Logistics LLC"]}
    placeholder = {"mechanism": "party", "values": ["COMPANY"]}
    non_party = {"mechanism": "free_text", "values": ["whatever"]}
    assert km.party_supersedable(real, chain_doc) is True
    assert km.party_supersedable(placeholder, chain_doc) is False
    assert km.party_supersedable(real, ancillary) is False
    assert km.party_supersedable(non_party, chain_doc) is False


def test_current_party_keys_novation_flips_the_family_counterparty():
    fam = [
        {"doc_id": "base", "document_type": "Agreement", "best_date": "1999-08-02",
         "ordinal": 0, "party_keys": {"licensee": "northwind logistics"}},
        {"doc_id": "side", "document_type": "Side Letter", "best_date": "2019-05-03",
         "ordinal": 0, "party_keys": {"licensee": "company"}},   # ancillary, never counts
        {"doc_id": "assign", "document_type": "Assignment", "best_date": "2020-10-01",
         "ordinal": 0, "party_keys": {"licensee": "fairhaven analytics"}},
    ]
    assert km.current_party_keys(fam) == {"licensee": "fairhaven analytics"}


def test_current_party_keys_skips_docs_without_a_signal():
    fam = [
        {"doc_id": "base", "document_type": "Agreement", "best_date": "1999-08-02",
         "ordinal": 0, "party_keys": {"licensee": "northwind logistics"}},
        {"doc_id": "amend", "document_type": "Amendment", "best_date": "2020-10-01",
         "ordinal": 1, "party_keys": {}},            # amendment silent on parties
    ]
    assert km.current_party_keys(fam) == {"licensee": "northwind logistics"}


def test_current_party_keys_tracks_each_role_independently():
    """The role that novates is domain vocabulary, so currency is resolved per
    role. A newer document naming only one of them must not blank the other."""
    fam = [
        {"doc_id": "base", "document_type": "agreement", "best_date": "2020-01-01",
         "ordinal": 0, "party_keys": {"licensor": "northwind", "licensee": "fairhaven"}},
        {"doc_id": "assign", "document_type": "assignment", "best_date": "2024-01-01",
         "ordinal": 0, "party_keys": {"licensee": "harbour point"}},
    ]
    assert km.current_party_keys(fam) == {"licensor": "northwind",
                                          "licensee": "harbour point"}


def test_current_party_keys_empty_family():
    assert km.current_party_keys([]) == {}


# --------------------------------------------------------------------------- #
# "Not Stated" is the absence of a statement, not a statement of absence.
# --------------------------------------------------------------------------- #
def test_states_a_value_rejects_the_not_stated_sentinel():
    assert km.states_a_value({"values": ["SGD 120,000"]}) is True
    assert km.states_a_value({"values": ["Not Stated"]}) is False
    assert km.states_a_value({"values": ["not  stated "]}) is False
    assert km.states_a_value({"values": []}) is False
    assert km.states_a_value({}) is False
    # One real value among sentinels still counts as a statement.
    assert km.states_a_value({"values": ["Not Stated", "SGD 120,000"]}) is True


def test_a_corrected_not_stated_does_not_supersede_the_earlier_value():
    """The one way "Not Stated" reaches a record is a verifier saying the model
    read a value into a document that does not contain one. Treating that as a
    statement would let the correction bury the real figure in the base
    document, which is the opposite of what the verifier meant.
    """
    fk = "commercial_terms.minimum_commitment"
    docs = [
        {"doc_id": "base", "fields": [{"full_key": fk, "values": ["SGD 120,000"]}]},
        {"doc_id": "amend", "fields": [{"full_key": fk, "values": ["Not Stated"]}]},
    ]
    ordered = [{"doc_id": d["doc_id"], "best_date": date, "ordinal": 0,
                "stated_fields": {f["full_key"] for f in d["fields"]
                                  if km.states_a_value(f)}}
               for d, date in zip(docs, ["2023-03-14", "2025-01-20"])]
    status = km.supersedence(km.order_family(ordered), {fk: 1})
    assert status[("base", fk)]["is_current"] is True
    assert ("amend", fk) not in status, "the sentinel never enters the walk"
