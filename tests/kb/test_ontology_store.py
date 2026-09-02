"""Unit tests for the SQL-backed ontology overlay store (pipeline/kb/ontology_store.py).

The store must compile its rows into EXACTLY the overlay-dict shape apply_overlay
consumes, survive concurrent-style upserts, keep attribution, and migrate a legacy
YAML overlay once. Pure sqlite in a tmp dir — free, no app DB touched.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline.kb import ontology_store as store


@pytest.fixture()
def cfg(tmp_path):
    return SimpleNamespace(storage_root=tmp_path)


def test_empty_store_is_empty_overlay(cfg):
    assert store.overlay_dict(cfg=cfg) == {}


def test_field_roundtrip_and_shape(cfg):
    fdef = {"title": "Payment Terms", "type": "text", "multiplicity": 1,
            "hint": "days to pay each invoice", "origin": "user",
            "source": {"mechanism": "llm"}}
    store.upsert_field("commercial_link", "payment_terms", fdef, by="a@x.com", cfg=cfg)
    ov = store.overlay_dict(cfg=cfg)
    assert ov["categories"]["commercial_link"]["fields"]["payment_terms"] == fdef
    who = store.attribution(cfg=cfg)["commercial_link.payment_terms"]
    assert who["updated_by"] == "a@x.com" and who["updated_at"]


def test_partial_edit_merges_over_prior_payload(cfg):
    store.upsert_field("c", "f", {"title": "A", "hint": "old"}, by="a@x.com", cfg=cfg)
    store.upsert_field("c", "f", {"hint": "new"}, merge=True, by="b@x.com", cfg=cfg)
    fdef = store.overlay_dict(cfg=cfg)["categories"]["c"]["fields"]["f"]
    assert fdef == {"title": "A", "hint": "new"}          # title survived the partial edit
    assert store.attribution(cfg=cfg)["c.f"]["updated_by"] == "b@x.com"


def test_delete_user_field_removes_row(cfg):
    store.upsert_field("c", "f", {"title": "A"}, cfg=cfg)
    store.delete_field("c", "f", base_field=False, cfg=cfg)
    assert store.overlay_dict(cfg=cfg) == {}


def test_delete_base_field_tombstones_and_readd_clears(cfg):
    store.delete_field("sla_performance", "ld_cap", base_field=True, by="a@x.com", cfg=cfg)
    assert store.overlay_dict(cfg=cfg)["deleted"] == ["sla_performance.ld_cap"]
    # Re-adding the field drops the tombstone.
    store.upsert_field("sla_performance", "ld_cap", {"title": "LD Cap"}, cfg=cfg)
    ov = store.overlay_dict(cfg=cfg)
    assert "deleted" not in ov
    assert "ld_cap" in ov["categories"]["sla_performance"]["fields"]


def test_sensitivity_rows_compile_into_overlay_shape(cfg):
    store.set_sensitivity("service_scope", "confidential", cfg=cfg)
    store.set_sensitivity("basic_contract_info", "confidential", "supplier_name", cfg=cfg)
    sens = store.overlay_dict(cfg=cfg)["sensitivity"]
    assert sens["categories"] == {"service_scope": "confidential"}
    assert sens["field_overrides"] == {"basic_contract_info.supplier_name": "confidential"}


def test_two_admins_editing_different_fields_do_not_clobber(cfg):
    # The failure mode of the old single-YAML overlay: last write wins for the FILE.
    # Rows are independent — both edits land.
    store.upsert_field("c", "f1", {"title": "One"}, by="admin1@x.com", cfg=cfg)
    store.upsert_field("c", "f2", {"title": "Two"}, by="admin2@x.com", cfg=cfg)
    fields = store.overlay_dict(cfg=cfg)["categories"]["c"]["fields"]
    assert set(fields) == {"f1", "f2"}


def test_migrate_yaml_imports_once_and_renames(cfg, tmp_path):
    p = tmp_path / "legacy_ops.overlay.yaml"
    p.write_text(
        "categories:\n"
        "  commercial_link:\n"
        "    fields:\n"
        "      payment_terms: {title: Payment Terms, type: text}\n"
        "deleted: [sla_performance.ld_cap]\n"
        "sensitivity:\n"
        "  categories: {service_scope: confidential}\n",
        encoding="utf-8")
    n = store.migrate_yaml(p, cfg=cfg)
    assert n == 3
    assert not p.exists()                          # renamed — can't double-apply
    assert p.with_suffix(".yaml.migrated").exists()
    ov = store.overlay_dict(cfg=cfg)
    assert "payment_terms" in ov["categories"]["commercial_link"]["fields"]
    assert ov["deleted"] == ["sla_performance.ld_cap"]
    assert ov["sensitivity"]["categories"] == {"service_scope": "confidential"}
    # Second call is a clean no-op.
    assert store.migrate_yaml(p, cfg=cfg) == 0
