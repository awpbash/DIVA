"""RBAC endpoint matrix over the real FastAPI app (TestClient).

Asserts the GATE per role — anonymous 401s, default 403s on walled surfaces,
confidential walled off review, admin passes. Graph-backed handlers are only
exercised with tokens that FAIL their gate (the dependency short-circuits
before any Neo4j touch), so the suite stays free and deterministic; admin
pass-through is asserted on disk/memory-backed routes only.
"""
from __future__ import annotations

import os

# The api package needs runtime config at import (review.py loads Config at
# module level). Provide harmless values BEFORE the first api import so the
# suite runs on a machine with no .env at all.
os.environ.setdefault("NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("NEO4J_USER", "neo4j")
os.environ.setdefault("NEO4J_PASSWORD", "test-password")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

import pytest


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    """TestClient + one session token per role, against a THROWAWAY app DB.
    Patches are UNDONE at teardown — leaking the lambdas would poison the
    vote-store unit tests that run after this module."""
    db_path = tmp_path_factory.mktemp("appdb") / "app.db"
    from api import appdb, review_votes
    mp = pytest.MonkeyPatch()
    mp.setattr(appdb, "_db_path", lambda: db_path)    # every _conn() re-resolves
    mp.setattr(review_votes, "_db_path", lambda cfg=None: db_path)
    # The lifespan's legacy-overlay migration targets REAL storage — keep the
    # suite side-effect-free (and deterministic) by no-opping it here.
    mp.setattr(review_votes, "migrate_legacy_overlays", lambda *a, **k: 0)

    from fastapi.testclient import TestClient
    from api.main import app

    with TestClient(app) as client:
        appdb.upsert_user("adm@test.local", "Adm", "", "admin", "All")
        appdb.upsert_user("conf@test.local", "Conf", "", "confidential", "All")
        appdb.upsert_user("def@test.local", "Def", "", "default", "All")
        appdb.upsert_user("ver@test.local", "Ver", "", "default", "All")
        appdb.update_account("ver@test.local", verifier=True)
        tokens = {
            "admin":        appdb.create_session("adm@test.local"),
            "confidential": appdb.create_session("conf@test.local"),
            "default":      appdb.create_session("def@test.local"),
            "verifier":     appdb.create_session("ver@test.local"),
        }
        yield client, tokens
    mp.undo()


def _get(rig, path: str, role: str | None) -> int:
    client, tokens = rig
    headers = {"X-User-Token": tokens[role]} if role else {}
    return client.get(path, headers=headers).status_code


# --------------------------------------------------------------------------- #
# Anonymous: everything meaningful is login-gated.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [
    "/documents", "/review/docs", "/review/heatmap", "/review/page/abc123/1",
    "/review/blocks/abc123/1", "/km/families", "/km/page/abc123/1",
    "/graph/overview", "/graph/hubs", "/graph/subgraph?evidence_ids=x",
    "/evidence/x", "/restricted-regions/abc123", "/policy", "/ontology",
    "/feedback/admin",
])
def test_anonymous_401(rig, path):
    assert _get(rig, path, None) == 401


def test_anonymous_feedback_post_401(rig):
    client, _ = rig
    r = client.post("/feedback", json={"category": "other", "message": "hi"})
    assert r.status_code == 401


def test_anonymous_chat_401(rig):
    client, _ = rig
    r = client.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Default role: chat furniture only — review/km/graph-explore/admin walled.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [
    "/review/docs", "/review/heatmap", "/review/page/abc123/1",
    "/review/blocks/abc123/1", "/km/families", "/km/page/abc123/1",
    "/graph/overview", "/graph/hubs", "/policy", "/ontology",
    "/feedback/admin",
])
def test_default_403_on_walled_surfaces(rig, path):
    assert _get(rig, path, "default") == 403


# --------------------------------------------------------------------------- #
# Confidential: knowledge surfaces open; review needs the verifier flag.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [
    "/review/docs", "/review/heatmap", "/review/page/abc123/1", "/policy", "/ontology",
])
def test_confidential_403_on_admin_surfaces(rig, path):
    assert _get(rig, path, "confidential") == 403


def test_confidential_passes_km_gate(rig):
    # Gate passes; the handler may 503 without a live graph — both prove
    # the dependency admitted the request.
    assert _get(rig, "/km/families", "confidential") not in (401, 403)


# --------------------------------------------------------------------------- #
# Approved verifier (default role + flag): review opens, other walls hold.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/review/docs", "/review/heatmap"])
def test_verifier_flag_opens_review(rig, path):
    assert _get(rig, path, "verifier") == 200


@pytest.mark.parametrize("path", ["/km/families", "/ontology", "/feedback/admin"])
def test_verifier_flag_opens_only_review(rig, path):
    # The flag is a review capability, not a clearance — everything else holds.
    assert _get(rig, path, "verifier") == 403


def test_verifier_flag_served_in_capabilities(rig):
    client, tokens = rig
    body = client.get("/auth/me", headers={"X-User-Token": tokens["verifier"]}).json()
    assert "review" in body["capabilities"]["tabs"]
    assert body["user"]["verifier"] is True


# --------------------------------------------------------------------------- #
# Admin: passes everywhere (asserted on disk/memory-backed routes).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [
    "/review/docs", "/review/heatmap", "/policy", "/ontology", "/auth/me",
])
def test_admin_200_on_disk_backed(rig, path):
    assert _get(rig, path, "admin") == 200


def test_capabilities_served_on_me(rig):
    client, tokens = rig
    body = client.get("/auth/me", headers={"X-User-Token": tokens["default"]}).json()
    assert body["capabilities"]["tabs"] == ["chat"]
    body = client.get("/auth/me", headers={"X-User-Token": tokens["admin"]}).json()
    assert "review" in body["capabilities"]["tabs"]


def test_vote_requires_verifier_flag(rig):
    client, tokens = rig
    # Unflagged non-admin vote → 403 before any disk write.
    r = client.post("/review/doc/abc123/field/x.y/verify",
                    json={"decision": "approve"},
                    headers={"X-User-Token": tokens["confidential"]})
    assert r.status_code == 403
    # A flagged verifier passes the gate; an unknown doc stops at 404 —
    # deterministic proof of admission without touching real records.
    r = client.post("/review/doc/abc123/field/x.y/verify",
                    json={"decision": "approve"},
                    headers={"X-User-Token": tokens["verifier"]})
    assert r.status_code == 404


def test_feedback_roundtrip(rig):
    """Any logged-in user files feedback; identity is stamped server-side;
    admin triages it. The whole loop against the throwaway app DB."""
    client, tokens = rig
    r = client.post("/feedback",
                    json={"category": "answer", "message": "the rate looks wrong",
                          "context": {"tab": "chat", "email": "spoofed@evil"}},
                    headers={"X-User-Token": tokens["default"]})
    assert r.status_code == 200
    fid = r.json()["id"]

    body = client.get("/feedback/admin",
                      headers={"X-User-Token": tokens["admin"]}).json()
    item = next(i for i in body["items"] if i["id"] == fid)
    assert item["email"] == "def@test.local"        # session identity, not the blob
    assert item["status"] == "new"
    assert body["counts"]["new"] >= 1

    r = client.put(f"/feedback/admin/{fid}", json={"status": "acknowledged"},
                   headers={"X-User-Token": tokens["admin"]})
    assert r.status_code == 200
    body = client.get("/feedback/admin?status=acknowledged",
                      headers={"X-User-Token": tokens["admin"]}).json()
    got = next(i for i in body["items"] if i["id"] == fid)
    assert got["status_by"] == "adm@test.local"

    # Garbage status → 404 (invalid transition), nothing changed.
    r = client.put(f"/feedback/admin/{fid}", json={"status": "wontfix"},
                   headers={"X-User-Token": tokens["admin"]})
    assert r.status_code == 404


def test_feedback_screenshot_roundtrip(rig, monkeypatch, tmp_path):
    """Upload a screenshot → file a report referencing it → only an admin can
    read it back. Attachment storage is redirected to a tmp dir."""
    client, tokens = rig
    from api.routes import feedback as fb_mod
    monkeypatch.setattr(fb_mod, "_attach_dir", lambda: tmp_path)

    r = client.post("/feedback/upload",
                    files=[("files", ("shot.png", b"\x89PNG-fake-bytes", "image/png"))],
                    headers={"X-User-Token": tokens["default"]})
    assert r.status_code == 200
    names = r.json()["names"]
    assert len(names) == 1 and names[0].endswith(".png")

    # Non-images are refused outright.
    r = client.post("/feedback/upload",
                    files=[("files", ("x.pdf", b"%PDF", "application/pdf"))],
                    headers={"X-User-Token": tokens["default"]})
    assert r.status_code == 400

    r = client.post("/feedback",
                    json={"category": "ui_ux", "message": "see the screenshot",
                          "attachments": names + ["../../etc/passwd", "zz.png"]},
                    headers={"X-User-Token": tokens["default"]})
    assert r.status_code == 200
    fid = r.json()["id"]

    body = client.get("/feedback/admin", headers={"X-User-Token": tokens["admin"]}).json()
    item = next(i for i in body["items"] if i["id"] == fid)
    assert item["attachments"] == names               # bogus names filtered out

    # Serving: admin 200, reporter themselves 403 (admin-only), traversal 404.
    ok = client.get(f"/feedback/admin/attachment/{names[0]}",
                    headers={"X-User-Token": tokens["admin"]})
    assert ok.status_code == 200 and ok.content == b"\x89PNG-fake-bytes"
    assert client.get(f"/feedback/admin/attachment/{names[0]}",
                      headers={"X-User-Token": tokens["default"]}).status_code == 403
    assert client.get("/feedback/admin/attachment/..%2Fapp.db",
                      headers={"X-User-Token": tokens["admin"]}).status_code == 404


# --------------------------------------------------------------------------- #
# Activity feed (the admin changelog): admin-only, merges all three histories.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role,code", [
    (None, 401), ("default", 403), ("confidential", 403), ("verifier", 403),
])
def test_activity_gate(rig, role, code):
    assert _get(rig, "/admin/activity", role) == code


@pytest.mark.parametrize("role,code", [
    (None, 401), ("default", 403), ("confidential", 403), ("verifier", 403),
])
def test_metrics_gate(rig, role, code):
    assert _get(rig, "/admin/metrics", role) == code


def test_metrics_counts_logins_and_resolves_names(rig):
    client, tokens = rig
    # A real login through the endpoint writes an append-only usage row
    # (sessions can't serve as the history — logout deletes its row).
    r = client.post("/auth/login", json={"email": "def@test.local"})
    assert r.status_code == 200
    from api import appdb
    appdb.log_usage("def@test.local", "chat", prompt_tokens=900,
                    completion_tokens=100, calls=4)

    body = client.get("/admin/metrics",
                      headers={"X-User-Token": tokens["admin"]}).json()
    me = next(u for u in body["users"] if u["email"] == "def@test.local")
    assert me["logins"] >= 1 and me["last_login"]
    assert me["name"] == "Def" and me["role"] == "default"
    assert body["totals"]["logins"] >= 1
    assert isinstance(body["daily"], list) and body["daily"]

    # Chart series: dense 7-day axis (quiet days zero-filled, not skipped).
    assert len(body["daily7"]) == 7
    assert [d for d in body["daily7"] if d["questions"] or d["tokens"]]
    mine = next(u for u in body["user_daily"] if u["email"] == "def@test.local")
    assert mine["name"] == "Def" and mine["tokens"] == 1000
    assert len(mine["series"]) == 7
    assert sum(s["questions"] for s in mine["series"]) == 1


def test_activity_merges_votes_events_and_feedback(rig):
    client, tokens = rig
    from api import appdb, review_votes
    review_votes.cast_vote("doc42", "cat.field_x", "ver@test.local", "approve")
    appdb.log_event("adm@test.local", "ontology", "added field", target="cat.field_x")
    # Feedback rows exist from the roundtrip tests above.

    items = client.get("/admin/activity",
                       headers={"X-User-Token": tokens["admin"]}).json()["items"]
    kinds = {i["kind"] for i in items}
    assert {"verification", "ontology", "feedback"} <= kinds
    vote = next(i for i in items if i["kind"] == "verification")
    assert vote["actor_name"] == "Ver"                # email resolved to a name
    assert "field x" in vote["target"]                # humanised field key
    assert items == sorted(items, key=lambda x: x["at"], reverse=True)


# --------------------------------------------------------------------------- #
# Exhaustive: EVERY route is login-gated unless it is on the public list.
#
# The hand-written parametrize list above is useful documentation but it cannot
# fail for a route nobody remembered to add, which is how GET /auth/accounts
# shipped with no gate at all: a leftover from a demo login picker that no
# longer exists, quietly serving every account's email address. Under
# passwordless sign-in an email IS the credential, so that was an open list of
# ways in.
#
# This sweep enumerates the app's own routing table instead. A new endpoint is
# gated by default, and making one public takes a deliberate edit here.
# --------------------------------------------------------------------------- #

# Public BY DESIGN, each for a stated reason.
PUBLIC_ROUTES = {
    ("GET", "/healthz"),        # liveness, polled by orchestrators with no creds
    ("GET", "/readyz"),         # readiness, same
    ("GET", "/branding"),       # the login screen renders before any session
    ("POST", "/auth/login"),    # the way in
    ("POST", "/auth/logout"),   # idempotent, and refusing it would strand a bad token
}

# Path params filled with values that are syntactically fine and match nothing,
# so a route that DOES pass its gate still cannot do real work.
_PARAM_FILL = {
    "doc_id": "aaaaaaaaaaaaaaaa", "evidence_id": "x", "page_no": "1",
    "email": "nobody@example.com", "group": "g", "fid": "1", "name": "n",
    "category": "c", "key": "k", "folder_id": "f", "field_key": "c.k",
}


def _declared_routes():
    """(method, template, concrete_path) for every API route the app declares."""
    import re

    from api.main import app
    out = []
    for r in app.routes:
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", None) or set()
        # The SPA catch-all serves the frontend bundle and must stay public.
        if not path or "{spa_path" in path:
            continue
        # FastAPI's own docs endpoints are not application routes.
        if path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
            continue
        concrete = re.sub(r"\{(\w+)(?::[^}]+)?\}",
                          lambda m: _PARAM_FILL.get(m.group(1), "x"), path)
        for m in sorted(methods - {"HEAD", "OPTIONS"}):
            out.append((m, path, concrete))
    return out


def test_the_route_table_is_not_empty():
    """A sweep over an empty list passes vacuously, which is worse than useless."""
    assert len(_declared_routes()) > 30


def test_every_route_is_gated_or_explicitly_public(rig):
    client, _ = rig
    leaks = []
    for method, template, concrete in _declared_routes():
        if (method, template) in PUBLIC_ROUTES:
            continue
        r = client.request(method, concrete, json={})
        if r.status_code != 401:
            leaks.append(f"{method} {template} -> {r.status_code}")
    assert not leaks, (
        "these routes answered an anonymous caller instead of 401:\n  "
        + "\n  ".join(leaks)
        + "\n\nAdd the auth dependency, or add it to PUBLIC_ROUTES with a reason.")


def test_public_routes_really_are_reachable(rig):
    """The allowlist must describe reality, not just excuse failures."""
    client, _ = rig
    for method, path in sorted(PUBLIC_ROUTES):
        if path == "/readyz":
            continue    # needs a live store; its gate-freeness is the point, not its answer
        r = client.request(method, path, json={"email": "nobody@example.com"})
        assert r.status_code != 404, f"{method} {path} is on the public list but does not exist"
