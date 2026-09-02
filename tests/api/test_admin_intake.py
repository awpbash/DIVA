"""Post-upload declaration editing: /admin/doc/{id}/intake and /folder.

The intake endpoint validates a PARTIAL patch merged over the existing
declaration with the exact upload rules, so an edit can never leave a
half-legal declaration. The folder endpoint moves a document between named
families, setting the sidecar group. Runs against the real FastAPI app with a
throwaway app DB and a tmp storage root patched into the admin routes. Graph
pushes and the KM refresh are best-effort seams and stay out of the way here
(no live store).
"""
from __future__ import annotations

import os

# Runtime config must exist BEFORE the first api import (review.py loads
# Config at module level), so the suite runs on a machine with no .env.
os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USER", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "test-password")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

import json
from types import SimpleNamespace

import pytest


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """TestClient + one admin session against a throwaway app DB, with the
    admin routes' storage root pointed at tmp_path."""
    db_path = tmp_path / "app.db"
    from api import appdb, review_votes
    monkeypatch.setattr(appdb, "_db_path", lambda: db_path)
    monkeypatch.setattr(review_votes, "_db_path", lambda cfg=None: db_path)
    monkeypatch.setattr(review_votes, "migrate_legacy_overlays", lambda *a, **k: 0)

    import api.routes.admin as admin_routes
    cfg = SimpleNamespace(storage_root=tmp_path)
    (tmp_path / "raw").mkdir(exist_ok=True)
    monkeypatch.setattr(admin_routes, "_CFG", cfg)
    # The free KM refresh needs a live store. The endpoints only KICK it, so
    # a no-op keeps the tests deterministic and store-free.
    monkeypatch.setattr(admin_routes, "_run_km_refresh", lambda doc_id: None)

    from fastapi.testclient import TestClient
    from api.main import app

    with TestClient(app) as client:
        appdb.upsert_user("adm@test.local", "Adm", "", "admin", "All")
        token = appdb.create_session("adm@test.local")
        yield client, {"X-User-Token": token}, cfg


def _mk_doc(cfg, doc_id, intake=None, group=None):
    raw = cfg.storage_root / "raw"
    (raw / f"{doc_id}.pdf").write_bytes(b"%PDF-1.4 test")
    sidecar = {"title": doc_id, "source_path": "test", "group": group}
    if intake:
        sidecar["intake"] = intake
    (raw / f"{doc_id}.meta.json").write_text(json.dumps(sidecar), encoding="utf-8")


# What a document may be DECLARED as is domain configuration, so take the
# active domain's first type rather than naming one. Writing "Contract" here
# passed against the domain this was written for and failed against the one the
# repository ships, which is the whole point of not writing it down.
from pipeline.kb.intake import doc_types  # noqa: E402

A_DOC_TYPE = doc_types()[0]
_BASE_INTAKE = {"document_type": A_DOC_TYPE, "document_date": "2026-01-01"}


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #
def test_anonymous_401_and_unknown_doc_404(rig):
    client, hdrs, cfg = rig
    assert client.post("/admin/doc/x/intake", json={}).status_code == 401
    assert client.post("/admin/doc/x/folder", json={}).status_code == 401
    assert client.post("/admin/doc/ghost/intake", json={},
                       headers=hdrs).status_code == 404
    assert client.post("/admin/doc/ghost/folder", json={},
                       headers=hdrs).status_code == 404


# --------------------------------------------------------------------------- #
# Intake patch validation (the merged declaration must pass the upload rules)
# --------------------------------------------------------------------------- #
def test_bad_relation_rejected(rig):
    client, hdrs, cfg = rig
    _mk_doc(cfg, "docbad", intake=dict(_BASE_INTAKE))
    r = client.post("/admin/doc/docbad/intake",
                    json={"relation": "replaces"}, headers=hdrs)
    assert r.status_code == 400
    assert "relation" in r.json()["detail"]


def test_link_relation_requires_an_existing_parent(rig):
    client, hdrs, cfg = rig
    _mk_doc(cfg, "child1", intake=dict(_BASE_INTAKE))
    # No parent at all.
    r = client.post("/admin/doc/child1/intake",
                    json={"relation": "amends"}, headers=hdrs)
    assert r.status_code == 400
    # A parent that is not in the corpus.
    r = client.post("/admin/doc/child1/intake",
                    json={"relation": "amends", "parent_doc_id": "ghost"},
                    headers=hdrs)
    assert r.status_code == 400
    assert "parent" in r.json()["detail"].lower()


def test_partial_patch_preserves_undeclared_keys(rig):
    client, hdrs, cfg = rig
    from pipeline.kb import intake as intake_mod
    from pipeline.kb import registry
    _mk_doc(cfg, "parentdoc9999")
    _mk_doc(cfg, "docp", intake={"relation": "amends",
                                 "parent_doc_id": "parentdoc9999",
                                 **_BASE_INTAKE})
    r = client.post("/admin/doc/docp/intake",
                    json={"document_date": "2026-02-02"}, headers=hdrs)
    assert r.status_code == 200
    ink = intake_mod.load_intake(cfg, "docp")
    assert ink["relation"] == "amends"                 # untouched by the patch
    assert ink["parent_doc_id"] == "parentdoc9999"     # untouched by the patch
    assert ink["document_type"] == A_DOC_TYPE           # untouched by the patch
    assert ink["document_date"] == "2026-02-02"        # the one change
    # The SQL projection follows the sidecar.
    assert registry.list_documents(cfg)["docp"]["document_date"] == "2026-02-02"


def test_link_relation_families_parent_and_child(rig):
    client, hdrs, cfg = rig
    from pipeline.kb import intake as intake_mod
    _mk_doc(cfg, "parentdoc1234")
    _mk_doc(cfg, "childdoc", intake=dict(_BASE_INTAKE))
    r = client.post("/admin/doc/childdoc/intake",
                    json={"relation": "amends", "parent_doc_id": "parentdoc1234"},
                    headers=hdrs)
    assert r.status_code == 200
    expected = "fam:" + "parentdoc1234"[:12]
    assert intake_mod.load_group(cfg, "parentdoc1234") == expected
    assert intake_mod.load_group(cfg, "childdoc") == expected
    assert intake_mod.load_intake(cfg, "childdoc")["relation"] == "amends"


# --------------------------------------------------------------------------- #
# Folder moves
# --------------------------------------------------------------------------- #
def test_unknown_folder_rejected(rig):
    client, hdrs, cfg = rig
    _mk_doc(cfg, "docu", intake=dict(_BASE_INTAKE))
    r = client.post("/admin/doc/docu/folder",
                    json={"folder_id": "fld:nope"}, headers=hdrs)
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Upload straight into a folder
# --------------------------------------------------------------------------- #
def test_upload_into_folder_files_the_document(rig, monkeypatch):
    client, hdrs, cfg = rig
    import api.routes.admin as admin_routes
    from pipeline.kb import intake as intake_mod
    from pipeline.kb import registry
    # The upload kicks the full extraction chain in a thread. Not under test.
    monkeypatch.setattr(admin_routes, "_run_extract_job", lambda doc_id: None)
    registry.upsert_folder("fld:uptest0001", "Upload fold", cfg=cfg)

    r = client.post("/admin/upload", headers=hdrs,
                    files={"file": ("new contract.pdf", b"%PDF-1.4 upload-test",
                                    "application/pdf")},
                    data={"folder_id": "fld:uptest0001",
                          "document_type": A_DOC_TYPE,
                          "document_date": "2026-01-01"})
    assert r.status_code == 200, r.text
    doc_id = r.json()["doc_id"]
    assert intake_mod.load_group(cfg, doc_id) == "fld:uptest0001"


def test_upload_unknown_folder_rejected(rig):
    client, hdrs, cfg = rig
    r = client.post("/admin/upload", headers=hdrs,
                    files={"file": ("x.pdf", b"%PDF-1.4 y", "application/pdf")},
                    data={"folder_id": "fld:ghost",
                          "document_type": A_DOC_TYPE,
                          "document_date": "2026-01-01"})
    assert r.status_code == 400
    assert "folder" in r.json()["detail"]
