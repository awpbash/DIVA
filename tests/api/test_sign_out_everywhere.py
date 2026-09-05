"""The admin-only /auth/accounts/{email}/sign-out-everywhere route
(api/routes/auth.py). Before this route existed, the only way to kill a
leaked or shared session token was to wait out the 30-day expiry or delete
the whole account. Same rig-style fixture as tests/api/test_auth_routes.py.
"""
from __future__ import annotations

import os

os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USER", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "test-password")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

import pytest


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """TestClient against a throwaway app DB, seeded with one admin and one
    default account."""
    db_path = tmp_path / "app.db"
    from api import appdb, review_votes
    monkeypatch.setattr(appdb, "_db_path", lambda: db_path)
    monkeypatch.setattr(review_votes, "_db_path", lambda cfg=None: db_path)
    monkeypatch.setattr(review_votes, "migrate_legacy_overlays", lambda *a, **k: 0)

    from fastapi.testclient import TestClient
    from api.main import app

    with TestClient(app) as client:
        appdb.upsert_user("admin@test.local", "Admin", "", "admin", "All")
        appdb.upsert_user("target@test.local", "Target", "", "default", "All")
        yield client


def _login(rig, email: str) -> str:
    return rig.post("/auth/login", json={"email": email}).json()["token"]


def test_admin_can_sign_a_target_out_everywhere(rig):
    admin_token = _login(rig, "admin@test.local")
    target_token = _login(rig, "target@test.local")
    assert rig.get("/auth/me", headers={"X-User-Token": target_token}).status_code == 200

    r = rig.post("/auth/accounts/target@test.local/sign-out-everywhere",
                headers={"X-User-Token": admin_token})
    assert r.status_code == 200
    assert r.json()["cleared"] == 1

    # The old token is dead, not just cosmetically "signed out".
    assert rig.get("/auth/me", headers={"X-User-Token": target_token}).status_code == 401


def test_it_clears_every_standing_token_for_the_account(rig):
    admin_token = _login(rig, "admin@test.local")
    t1 = _login(rig, "target@test.local")
    t2 = _login(rig, "target@test.local")

    r = rig.post("/auth/accounts/target@test.local/sign-out-everywhere",
                headers={"X-User-Token": admin_token})
    assert r.json()["cleared"] == 2
    assert rig.get("/auth/me", headers={"X-User-Token": t1}).status_code == 401
    assert rig.get("/auth/me", headers={"X-User-Token": t2}).status_code == 401


def test_the_admins_own_session_survives(rig):
    admin_token = _login(rig, "admin@test.local")
    _login(rig, "target@test.local")
    rig.post("/auth/accounts/target@test.local/sign-out-everywhere",
            headers={"X-User-Token": admin_token})
    assert rig.get("/auth/me", headers={"X-User-Token": admin_token}).status_code == 200


def test_a_non_admin_is_rejected(rig):
    target_token = _login(rig, "target@test.local")
    r = rig.post("/auth/accounts/target@test.local/sign-out-everywhere",
                headers={"X-User-Token": target_token})
    assert r.status_code == 403


def test_a_non_admin_cannot_use_it_even_on_their_own_account(rig):
    # The gate is role-based, not "only when acting on someone else".
    target_token = _login(rig, "target@test.local")
    r = rig.post("/auth/accounts/admin@test.local/sign-out-everywhere",
                headers={"X-User-Token": target_token})
    assert r.status_code == 403
