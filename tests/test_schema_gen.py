"""pipeline/schema_gen.py — the from-scratch wizard path's file generator.

Runs the SAME two live validators the wizard route uses
(`pipeline.extraction.pack.load`, `pipeline.kb.opsview_spec.load`) against
generated output, isolated into a throwaway configs/ tree (same pattern as
tests/extraction/test_active_domain.py's `_isolate`) so nothing here ever
touches the real repo's configs/analyzers, configs/ontology, configs/packs
or configs/views. `_base.yaml`, `_universal/analyzer.yaml` and the analyzer
JSON schema are copied in from the real repo, since a generated pack/analyzer
`extends` them.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pipeline import schema_gen as sg

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def isolated(monkeypatch, tmp_path):
    """A throwaway configs/ tree wired into every module that reads one, so
    a generated domain can be written and validated without touching the
    real repo. Returns the tmp configs root."""
    configs = tmp_path / "configs"
    (configs / "analyzers").mkdir(parents=True)
    (configs / "ontology").mkdir(parents=True)
    (configs / "packs").mkdir(parents=True)
    (configs / "views").mkdir(parents=True)
    (configs / "schemas").mkdir(parents=True)

    shutil.copytree(REPO / "configs" / "analyzers" / "_universal",
                    configs / "analyzers" / "_universal")
    shutil.copy(REPO / "configs" / "packs" / "_base.yaml", configs / "packs" / "_base.yaml")
    shutil.copy(REPO / "configs" / "schemas" / "analyzer.schema.json",
               configs / "schemas" / "analyzer.schema.json")
    # _load_pipeline_defaults() validates this against a full pydantic model
    # (vision/load/snippet/... all required) — copy the real one rather than
    # hand-rolling a fixture that has to track that model's required fields.
    shutil.copy(REPO / "configs" / "pipeline.yaml", configs / "pipeline.yaml")

    import pipeline.extraction.loader as loader_mod
    import pipeline.extraction.pack as pack_mod
    import pipeline.kb.opsview_spec as opsview_mod
    monkeypatch.setattr(loader_mod, "CONFIGS_DIR", configs)
    monkeypatch.setattr(loader_mod, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(pack_mod, "PACKS_DIR", configs / "packs")
    monkeypatch.setattr(opsview_mod, "_VIEWS_DIR", configs / "views")

    # Every cache that would otherwise serve a stale read across tests/cases
    # (see pipeline/schema_gen.py's module docstring on retry-safety).
    pack_mod.load.cache_clear()
    opsview_mod.party_roles.cache_clear()
    loader_mod.load_all(refresh=True)
    yield configs


def _write(configs: Path, gd: sg.GeneratedDomain) -> None:
    import pipeline.extraction.loader as loader_mod
    import pipeline.extraction.pack as pack_mod
    import pipeline.kb.opsview_spec as opsview_mod
    (configs / "analyzers" / gd.domain).mkdir(parents=True, exist_ok=True)
    (configs / "analyzers" / gd.domain / "analyzer.yaml").write_text(
        sg.dump_yaml(gd.analyzer), encoding="utf-8")
    (configs / "ontology" / f"{gd.domain}.yaml").write_text(
        sg.dump_yaml(gd.ontology), encoding="utf-8")
    (configs / "packs" / f"{gd.domain}.yaml").write_text(
        sg.dump_yaml(gd.pack), encoding="utf-8")
    (configs / "views" / f"{gd.domain}_ops.yaml").write_text(
        sg.dump_yaml(gd.ops_view), encoding="utf-8")
    pack_mod.load.cache_clear()
    opsview_mod.party_roles.cache_clear()
    loader_mod.load_all(refresh=True)


def _validate(configs: Path, domain: str):
    from pipeline.extraction.pack import load as load_pack
    from pipeline.kb.opsview_spec import load as load_view
    return load_pack(domain), load_view(doctype=domain)


# --------------------------------------------------------------------------- #
# P3.9 verification: no toggles, confidential + totals only, the full set.
# --------------------------------------------------------------------------- #
def test_no_toggles_plain_fields_only_validates(isolated):
    fields = [sg.DraftField(key="reference_number", title="Reference Number",
                            type="text", hint="the internal reference number")]
    gd = sg.generate("plain_domain", fields)
    _write(isolated, gd)
    pack, view = _validate(isolated, gd.domain)
    assert len(view.fields) == 1
    assert view.fields[0].mechanism == "llm"


def test_confidential_and_totals_validate(isolated):
    fields = [
        sg.DraftField(key="reference_number", title="Reference Number", type="text",
                      hint="ref", confidential=True),
        sg.DraftField(key="total_amount", title="Total Amount", type="value",
                      hint="the total amount", category="commercial"),
    ]
    gd = sg.generate("money_domain", fields)
    _write(isolated, gd)
    pack, view = _validate(isolated, gd.domain)
    ref = next(f for f in view.fields if f.key == "reference_number")
    assert ref.sensitivity == "confidential"
    total = next(f for f in view.fields if f.key == "total_amount")
    assert total.type == "value"          # sufficient for aggregation — see km.py:parse_numbers


def test_full_set_party_matching_and_amendment_chain_validates(isolated):
    fields = [
        sg.DraftField(key="landlord_name", title="Landlord", type="text",
                      hint="the landlord", category="parties"),
        sg.DraftField(key="tenant_name", title="Tenant", type="text",
                      hint="the tenant", category="parties"),
        sg.DraftField(key="monthly_rent", title="Monthly Rent", type="value",
                      hint="the monthly rent", category="commercial"),
    ]
    gd = sg.generate(
        "lease_domain", fields,
        amendment=sg.AmendmentChainToggle(enabled=True),
        party=sg.PartyMatchingToggle(enabled=True,
                                     field_keys=("landlord_name", "tenant_name")))
    _write(isolated, gd)
    pack, view = _validate(isolated, gd.domain)
    assert len(view.fields) == 3 + 5      # content fields + the 5 fixed amendment fields
    landlord = next(f for f in view.fields if f.key == "landlord_name")
    assert landlord.source == {"mechanism": "party", "role": "landlord_name"}
    assert "Party" in pack.fact_labels
    from pipeline.kb.opsview_spec import missing_field_roles
    assert missing_field_roles(gd.domain) == {}   # every chain role maps to a declared field


def test_amendment_chain_field_keys_are_reserved(isolated):
    fields = [sg.DraftField(key="document_type", title="Document Type", type="text")]
    with pytest.raises(sg.SchemaGenError, match="reserved"):
        sg.generate("bad_domain", fields, amendment=sg.AmendmentChainToggle(enabled=True))


def test_party_matching_requires_a_field_from_the_list(isolated):
    fields = [sg.DraftField(key="a", title="A", type="text")]
    with pytest.raises(sg.SchemaGenError, match="not in the field list"):
        sg.generate("bad_domain", fields,
                   party=sg.PartyMatchingToggle(enabled=True, field_keys=("nope",)))


def test_party_matching_on_with_no_fields_named_is_an_error(isolated):
    fields = [sg.DraftField(key="a", title="A", type="text")]
    with pytest.raises(sg.SchemaGenError, match="no fields were marked"):
        sg.generate("bad_domain", fields, party=sg.PartyMatchingToggle(enabled=True))


# --------------------------------------------------------------------------- #
# Field-list validation (no isolation needed — pure, no file I/O)
# --------------------------------------------------------------------------- #
def test_empty_field_list_is_rejected():
    with pytest.raises(sg.SchemaGenError, match="at least one field"):
        sg.generate("x", [])


def test_duplicate_field_keys_are_rejected():
    fields = [sg.DraftField(key="a", title="A", type="text"),
             sg.DraftField(key="a", title="A again", type="text")]
    with pytest.raises(sg.SchemaGenError, match="duplicate"):
        sg.generate("x", fields)


def test_enum_field_without_values_is_rejected():
    fields = [sg.DraftField(key="a", title="A", type="enum")]
    with pytest.raises(sg.SchemaGenError, match="value"):
        sg.generate("x", fields)


def test_bad_type_is_rejected():
    fields = [sg.DraftField(key="a", title="A", type="equipment")]  # not a _USER_TYPES member
    with pytest.raises(sg.SchemaGenError, match="type"):
        sg.generate("x", fields)


def test_slugify_matches_the_ontology_field_key_rule():
    from api.routes.ontology import FieldBody
    # Same cleaning rule as api/routes/ontology.py:add_field, so a from-scratch
    # field and a runtime-added field turn identical titles into identical keys.
    assert sg.slugify("Payment Terms") == "payment_terms"
    assert sg.slugify("Payment Terms").isidentifier()
    assert FieldBody  # imported only to prove the module still exists at this path


def test_slug_domain_matches_the_analyzer_id_pattern():
    import re
    pattern = re.compile(r"^_?[a-z][a-z0-9_]*$")
    assert pattern.match(sg.slug_domain("My New Domain!!"))
    assert pattern.match(sg.slug_domain("123 numbers first"))
