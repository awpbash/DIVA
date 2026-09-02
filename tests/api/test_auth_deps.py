"""Pure unit tests for the auth capability gates (api/routes/auth.py) and the
chat role resolution (api/routes/chat.py) — the functions every RBAC decision
runs through. No app, no DB, no network."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from api.routes.auth import (capabilities_for, require_admin, require_clearance,
                             require_verifier)


def _user(role: str, verifier: bool = False) -> dict:
    return {"email": f"{role}@test", "role": role, "verifier": int(verifier)}


# --------------------------------------------------------------------------- #
# require_admin / require_clearance / require_verifier
# --------------------------------------------------------------------------- #
def test_require_admin_passes_admin_only():
    assert require_admin(user=_user("admin"))["role"] == "admin"
    for role in ("confidential", "default", ""):
        with pytest.raises(HTTPException) as e:
            require_admin(user=_user(role))
        assert e.value.status_code == 403


def test_require_clearance_admin_and_confidential():
    assert require_clearance(user=_user("admin"))["role"] == "admin"
    assert require_clearance(user=_user("confidential"))["role"] == "confidential"
    for role in ("default", "general", ""):
        with pytest.raises(HTTPException) as e:
            require_clearance(user=_user(role))
        assert e.value.status_code == 403


def test_require_verifier_admins_and_flagged_accounts():
    # Admins always may vote; any role passes once flagged onto the approved list.
    assert require_verifier(user=_user("admin"))["role"] == "admin"
    assert require_verifier(user=_user("default", verifier=True))["role"] == "default"
    assert require_verifier(user=_user("confidential", verifier=True))["role"] == "confidential"
    for role in ("confidential", "default", ""):
        with pytest.raises(HTTPException) as e:
            require_verifier(user=_user(role))
        assert e.value.status_code == 403


# --------------------------------------------------------------------------- #
# capabilities — the server-declared tab matrix (per account, not just role)
# --------------------------------------------------------------------------- #
def test_capabilities_matrix():
    assert capabilities_for(_user("admin"))["tabs"] == [
        "chat", "explore", "review", "knowledge", "ontology", "admin"]
    assert capabilities_for(_user("confidential"))["tabs"] == ["chat", "explore", "knowledge"]
    assert capabilities_for(_user("default"))["tabs"] == ["chat"]


def test_capabilities_unknown_role_falls_closed():
    # An unknown / missing role gets the most-restricted tab list.
    assert capabilities_for(_user("superuser"))["tabs"] == ["chat"]
    assert capabilities_for(None)["tabs"] == ["chat"]


def test_capabilities_verifier_flag_opens_review_tab():
    assert capabilities_for(_user("default", verifier=True))["tabs"] == ["chat", "review"]
    assert capabilities_for(_user("confidential", verifier=True))["tabs"] == [
        "chat", "review", "explore", "knowledge"]
    # Admin already has review — the flag must not duplicate it.
    assert capabilities_for(_user("admin", verifier=True))["tabs"].count("review") == 1


# --------------------------------------------------------------------------- #
# chat role resolution — session role, admin-only impersonation
# --------------------------------------------------------------------------- #
def test_effective_role_admin_may_impersonate():
    from api.routes.chat import _effective_role
    assert _effective_role(_user("admin"), "default") == "default"
    assert _effective_role(_user("admin"), None) == "admin"


def test_effective_role_non_admin_request_ignored():
    from api.routes.chat import _effective_role
    # A default user claiming 'admin'/'finance' in the request is IGNORED —
    # the session role always wins for non-admins.
    assert _effective_role(_user("default"), "admin") == "default"
    assert _effective_role(_user("default"), "finance") == "default"
    assert _effective_role(_user("confidential"), "admin") == "confidential"
