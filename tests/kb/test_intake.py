"""Declared upload metadata: sidecar merge semantics + family linking."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pipeline.kb import intake


@pytest.fixture()
def cfg(tmp_path):
    (tmp_path / "raw").mkdir()
    return SimpleNamespace(storage_root=tmp_path)


def _write_sidecar(cfg, doc_id, data):
    (cfg.storage_root / "raw" / f"{doc_id}.meta.json").write_text(
        json.dumps(data), encoding="utf-8")


def test_merged_sidecar_replaces_intake_and_never_clobbers_group():
    existing = {"title": "t", "group": "Family X",
                "intake": {"customer_id": "OLD"}}
    out = intake.merged_sidecar(existing,
                                intake={"customer_id": "NEW", "block_id": None},
                                group="fam:abc")
    assert out["intake"] == {"customer_id": "NEW"}   # wholesale replace, no None keys
    assert out["group"] == "Family X"                 # folder-scanned family wins


def test_merged_sidecar_fills_missing_group():
    out = intake.merged_sidecar({"title": "t", "group": None},
                                intake={}, group="fam:abc")
    assert out["group"] == "fam:abc"


def test_save_and_load_roundtrip(cfg):
    _write_sidecar(cfg, "d1", {"title": "Doc 1", "group": None})
    intake.save_intake(cfg, "d1",
                       intake={"customer_id": "C1", "relation": "novates",
                               "parent_doc_id": "d0", "declared_by": "a@x"},
                       group="fam:d0")
    assert intake.load_intake(cfg, "d1")["customer_id"] == "C1"
    assert intake.load_group(cfg, "d1") == "fam:d0"
    # Title untouched by the merge.
    raw = json.loads((cfg.storage_root / "raw" / "d1.meta.json").read_text("utf-8"))
    assert raw["title"] == "Doc 1"


def test_family_for_parent_prefers_parent_group(cfg):
    _write_sidecar(cfg, "parent1", {"title": "p", "group": "Dummy Contract 4"})
    assert intake.family_for_parent(cfg, "parent1") == "Dummy Contract 4"
    _write_sidecar(cfg, "parent2", {"title": "p", "group": None})
    assert intake.family_for_parent(cfg, "parent2") == "fam:parent2"


def test_ensure_group_backfills_but_never_overwrites(cfg):
    _write_sidecar(cfg, "p", {"title": "p", "group": None})
    assert intake.ensure_group(cfg, "p", "fam:p") is True
    assert intake.load_group(cfg, "p") == "fam:p"
    assert intake.ensure_group(cfg, "p", "fam:other") is False
    assert intake.load_group(cfg, "p") == "fam:p"


def test_link_relation():
    assert intake.link_relation("novates") is True
    assert intake.link_relation("amends") is True
    assert intake.link_relation("standalone") is False
    assert intake.link_relation(None) is False


def test_load_intake_missing_sidecar_is_empty(cfg):
    assert intake.load_intake(cfg, "ghost") == {}
    assert intake.load_group(cfg, "ghost") is None
