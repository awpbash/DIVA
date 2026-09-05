"""The real /auth/login and /auth/logout routes, end to end through the
FastAPI app. Every other test that needs a session manufactures one directly
with appdb.create_session(...), which is fine for testing what a session can
DO but never exercises login itself, so a regression in the login route (an
account-enumeration difference, a token that survives logout, a missing
throttle) had nothing to catch it.
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
    """TestClient against a throwaway app DB, with one seeded account. The
    rate limiter's state resets on every app startup (api/main.py's
    lifespan), which TestClient(app) triggers here same as a real boot."""
    db_path = tmp_path / "app.db"
    from api import appdb, review_votes
    monkeypatch.setattr(appdb, "_db_path", lambda: db_path)
    monkeypatch.setattr(review_votes, "_db_path", lambda cfg=None: db_path)
    monkeypatch.setattr(review_votes, "migrate_legacy_overlays", lambda *a, **k: 0)

    from fastapi.testclient import TestClient
    from api.main import app

    with TestClient(app) as client:
        appdb.upsert_user("known@test.local", "Known Person", "", "default", "All")
        yield client


def test_login_with_a_seeded_email_succeeds(rig):
    r = rig.post("/auth/login", json={"email": "known@test.local"})
    assert r.status_code == 200
    body = r.json()
    assert body["token"]
    assert body["user"]["email"] == "known@test.local"
    assert body["user"]["role"] == "default"


def test_login_with_an_unknown_email_is_rejected(rig):
    r = rig.post("/auth/login", json={"email": "nobody@test.local"})
    assert r.status_code == 401


def test_logout_actually_invalidates_the_token(rig):
    token = rig.post("/auth/login", json={"email": "known@test.local"}).json()["token"]
    # The token works before logout...
    assert rig.get("/auth/me", headers={"X-User-Token": token}).status_code == 200
    r = rig.post("/auth/logout", headers={"X-User-Token": token})
    assert r.status_code == 200
    # ...and is rejected afterward, not just cosmetically "logged out" client-side.
    assert rig.get("/auth/me", headers={"X-User-Token": token}).status_code == 401


def test_login_is_throttled_regardless_of_outcome(rig):
    """Sign-in is passwordless, so an unthrottled login route is a free
    account-email scanner. The limit (10/60s, api/ratelimit.py) must apply
    the same way to a real email as to a made-up one, or an attacker could
    tell the two apart by which one gets rate limited first."""
    for _ in range(10):
        rig.post("/auth/login", json={"email": "nobody@test.local"})
    r = rig.post("/auth/login", json={"email": "known@test.local"})
    assert r.status_code == 429
