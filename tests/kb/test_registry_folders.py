"""Folders: named contract families over the existing sidecar group mechanism.

The folder_id IS the group string, so a folder only NAMES a family that
already exists structurally. These tests cover the SQL CRUD, the boot-time
sync from sidecar groups (idempotent, admin names preserved), the id minting
for admin-created folders, and the set_group sidecar move helper.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pipeline.kb import intake, registry


@pytest.fixture()
def cfg(tmp_path):
    (tmp_path / "raw").mkdir()
    return SimpleNamespace(storage_root=tmp_path)


def _sidecar(cfg, doc_id, data):
    (cfg.storage_root / "raw" / f"{doc_id}.meta.json").write_text(
        json.dumps(data), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Folder id minting
# --------------------------------------------------------------------------- #
def test_mint_folder_id_shape():
    fid = registry.mint_folder_id("Northwind family", at="2026-07-17T00:00:00")
    assert fid.startswith("fld:")
    assert len(fid) == len("fld:") + 10
    assert all(ch in "0123456789abcdef" for ch in fid[4:])
    # Deterministic for the same seed, fresh for a different timestamp.
    assert fid == registry.mint_folder_id("Northwind family", at="2026-07-17T00:00:00")
    assert fid != registry.mint_folder_id("Northwind family", at="2026-07-17T00:00:01")


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def test_folder_crud_roundtrip(cfg):
    fid = registry.mint_folder_id("Fam A")
    registry.upsert_folder(fid, "Fam A", by="admin@x", cfg=cfg)
    f = registry.get_folder(fid, cfg=cfg)
    assert f["name"] == "Fam A"
    assert f["active"] == 1
    assert f["updated_by"] == "admin@x"
    # Rename edits in place, no duplicate row.
    registry.upsert_folder(fid, "Family A", cfg=cfg)
    assert len(registry.list_folders(cfg, include_inactive=True)) == 1
    f = registry.get_folder(fid, cfg=cfg)
    assert f["name"] == "Family A"


def test_retire_and_restore_is_soft(cfg):
    registry.upsert_folder("fld:aaaaaaaaaa", "A", cfg=cfg)
    registry.upsert_folder("fld:bbbbbbbbbb", "B", cfg=cfg)
    assert registry.set_folder_active("fld:aaaaaaaaaa", False, by="admin@x", cfg=cfg)
    assert [f["folder_id"] for f in registry.list_folders(cfg)] == ["fld:bbbbbbbbbb"]
    # Soft retire: the row survives and stays fetchable by id.
    assert len(registry.list_folders(cfg, include_inactive=True)) == 2
    assert registry.get_folder("fld:aaaaaaaaaa", cfg=cfg)["active"] == 0
    assert registry.set_folder_active("fld:aaaaaaaaaa", True, cfg=cfg)
    assert len(registry.list_folders(cfg)) == 2


def test_set_folder_active_unknown_id_returns_false(cfg):
    assert registry.set_folder_active("fld:nope", False, cfg=cfg) is False


# --------------------------------------------------------------------------- #
# Sync from sidecar groups (boot path)
# --------------------------------------------------------------------------- #
def test_sync_creates_one_row_per_distinct_group(cfg):
    _sidecar(cfg, "d1", {"title": "Base", "group": "Dummy Contract 4"})
    _sidecar(cfg, "d2", {"title": "Amend", "group": "Dummy Contract 4"})
    _sidecar(cfg, "d3", {"title": "Loose", "group": None})
    assert registry.sync_folders_from_groups(cfg) == 1
    f = registry.get_folder("Dummy Contract 4", cfg=cfg)
    assert f is not None
    assert f["name"] == "Dummy Contract 4"
    # Idempotent: a second pass creates nothing.
    assert registry.sync_folders_from_groups(cfg) == 0


def test_sync_names_fam_groups_after_the_parent_title(cfg):
    parent_id = "abcdef123456" + "0" * 52
    _sidecar(cfg, parent_id, {"title": "Northwind Base Agreement",
                              "group": "fam:abcdef123456"})
    _sidecar(cfg, "child", {"title": "First Supplement",
                            "group": "fam:abcdef123456"})
    assert registry.sync_folders_from_groups(cfg) == 1
    f = registry.get_folder("fam:abcdef123456", cfg=cfg)
    assert f["name"] == "Northwind Base Agreement"


def test_sync_fam_group_without_a_parent_falls_back_to_the_hash(cfg):
    _sidecar(cfg, "orphan", {"title": "T", "group": "fam:beadfeed9999"})
    registry.sync_folders_from_groups(cfg)
    assert registry.get_folder("fam:beadfeed9999", cfg=cfg)["name"] == "beadfeed9999"


def test_sync_never_overwrites_an_admin_set_name(cfg):
    _sidecar(cfg, "d1", {"title": "Base", "group": "fam:abc999888777"})
    registry.sync_folders_from_groups(cfg)
    registry.upsert_folder("fam:abc999888777", "Renamed by admin",
                           by="admin@x", cfg=cfg)
    assert registry.sync_folders_from_groups(cfg) == 0
    assert registry.get_folder("fam:abc999888777",
                               cfg=cfg)["name"] == "Renamed by admin"


def test_sidecar_groups_mapping(cfg):
    _sidecar(cfg, "d1", {"group": "G1"})
    _sidecar(cfg, "d2", {"group": None})
    _sidecar(cfg, "d3", {"group": "G2"})
    assert registry.sidecar_groups(cfg) == {"d1": "G1", "d3": "G2"}


# --------------------------------------------------------------------------- #
# set_group: the sidecar move helper behind "Organise"
# --------------------------------------------------------------------------- #
def test_set_group_roundtrip_overwrite_and_clear(cfg):
    _sidecar(cfg, "d1", {"title": "Doc", "group": None,
                         "intake": {"document_type": "Contract"}})
    intake.set_group(cfg, "d1", "fld:1234567890")
    assert intake.load_group(cfg, "d1") == "fld:1234567890"
    # Unlike ensure_group, a move OVERWRITES the family.
    intake.set_group(cfg, "d1", "fld:aaaaaaaaaa")
    assert intake.load_group(cfg, "d1") == "fld:aaaaaaaaaa"
    # Everything else in the sidecar survives the move.
    raw = json.loads((cfg.storage_root / "raw" / "d1.meta.json")
                     .read_text(encoding="utf-8"))
    assert raw["title"] == "Doc"
    assert raw["intake"] == {"document_type": "Contract"}
    # Moving out of every folder clears the group.
    intake.set_group(cfg, "d1", None)
    assert intake.load_group(cfg, "d1") is None


def test_set_group_creates_a_sidecar_for_cli_era_docs(cfg):
    intake.set_group(cfg, "ghost", "fld:abcabcabca")
    assert intake.load_group(cfg, "ghost") == "fld:abcabcabca"
    raw = json.loads((cfg.storage_root / "raw" / "ghost.meta.json")
                     .read_text(encoding="utf-8"))
    assert raw["title"] == "ghost"
