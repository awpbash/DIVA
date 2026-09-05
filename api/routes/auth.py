"""api/routes/auth.py — passwordless email login backed by the accounts table.

Accounts live in `storage/app.db`. The first one is created at boot from
`BOOTSTRAP_ADMIN_EMAIL`, and an admin invites everyone else from the dashboard.
Logging in with a known email mints a session and returns that account's role
(admin / confidential / default), which then drives access control across the
app.

Sign-in has no password step, so an email address IS the credential. That is
fine on a single operator's machine and is NOT authentication for a shared
network. See SECURITY.md before exposing an instance. The access RULES below
hold either way.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .. import appdb

router = APIRouter(prefix="/auth", tags=["auth"])


def _public(user: dict) -> dict:
    return {"email": user["email"], "name": user.get("name"), "title": user.get("title"),
            "role": user.get("role"), "zone": user.get("zone"),
            "verifier": bool(user.get("verifier"))}


_ROLES = {"admin", "confidential", "default"}


def current_user(x_user_token: str | None = Header(default=None)) -> dict:
    """FastAPI dependency: resolve the logged-in user from the session token, or 401."""
    user = appdb.session_user(x_user_token)
    if not user:
        raise HTTPException(401, "not logged in")
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    """Capability gate: only an Admin account may manage accounts / the admin dashboard."""
    if user.get("role") != "admin":
        raise HTTPException(403, "admin only")
    return user


def require_verifier(user: dict = Depends(current_user)) -> dict:
    """Capability gate for the review surface: admins, plus any account an admin
    flagged onto the approved-verifier list. Verification is a vote, so several
    engineers need in — but only vetted ones (their approvals stamp the KB trusted)."""
    if user.get("role") == "admin" or user.get("verifier"):
        return user
    raise HTTPException(403, "verifier access required — ask an admin to add you "
                             "to the approved verifier list")


_CLEARED_ROLES = {"admin", "confidential"}


def require_clearance(user: dict = Depends(current_user)) -> dict:
    """Capability gate for cleared-eyes surfaces (Knowledge base, Explore graph):
    Confidential and Admin accounts only. Default accounts get chat, where
    role-filtering applies per retrieval — not the raw knowledge surfaces."""
    if user.get("role") not in _CLEARED_ROLES:
        raise HTTPException(403, "requires confidential or admin access")
    return user


# Which tabs each role may use. Served with /auth/me + /auth/login so the
# frontend renders exactly what the server will allow — one source of truth.
# An admin-editable rules table can replace this dict later without touching
# the frontend (it just reads `capabilities.tabs`).
_TABS_FOR_ROLE = {
    "admin":        ["chat", "explore", "review", "knowledge", "ontology", "admin"],
    "confidential": ["chat", "explore", "knowledge"],
    "default":      ["chat"],
}


def capabilities_for(user: dict | None) -> dict:
    role = str((user or {}).get("role") or "")
    tabs = list(_TABS_FOR_ROLE.get(role, _TABS_FOR_ROLE["default"]))
    # The approved-verifier flag opens the Review tab regardless of role —
    # what a verifier can SEE there is still clearance-filtered server-side.
    if (user or {}).get("verifier") and "review" not in tabs:
        tabs.insert(1, "review")
    return {"tabs": tabs}


@router.get("/accounts")
def list_accounts(_admin: dict = Depends(require_admin)) -> dict:
    """Every account, for the admin dashboard's account manager.

    ADMIN ONLY, and that gate is load-bearing rather than tidy. Sign-in is
    passwordless, so an account's email address IS its credential: an open list
    of accounts is an open list of ways in, and the login screen deliberately
    offers no account picker for exactly this reason. This route once had no
    gate at all, left over from a demo picker that no longer exists.
    """
    return {"accounts": [_public(u) for u in appdb.list_users()]}


class LoginBody(BaseModel):
    email: str


@router.post("/login")
def login(body: LoginBody) -> dict:
    """Log in by email (no password — demo). Unknown emails are rejected, so access is
    controlled by the seeded RBAC account list, not by whatever you type."""
    user = appdb.get_user(body.email)
    if not user:
        # "Ask an admin" is a dead end for a solo operator who IS the admin and
        # is simply locked out (wrong email, or the account list came from an
        # earlier machine). Point at the one recovery path that always exists:
        # a shell into the container, not a person to ask.
        raise HTTPException(
            401, "no account for this email. Ask an admin to add you. If "
                 "you're the only admin, run "
                 "`docker compose exec app python -m scripts.accounts list` "
                 "to see the registered accounts.")
    token = appdb.create_session(user["email"])
    appdb.log_usage(user["email"], "login")   # append-only sign-in history (metrics)
    return {"token": token, "user": _public(user),
            "capabilities": capabilities_for(user)}


@router.get("/me")
def me(user: dict = Depends(current_user)) -> dict:
    return {"user": _public(user), "capabilities": capabilities_for(user)}


@router.post("/logout")
def logout(x_user_token: str | None = Header(default=None)) -> dict:
    if x_user_token:
        appdb.delete_session(x_user_token)
    return {"ok": True}


# --- Account management (Admin only) --------------------------------------- #
class AccountEditBody(BaseModel):
    """Partial edit — only the fields sent change. `new_email` renames the account
    (sessions + chat history follow it). `verifier` grants/revokes the approved-
    verifier flag (the right to vote on field verifications)."""
    role: str | None = None
    name: str | None = None
    title: str | None = None
    new_email: str | None = None
    verifier: bool | None = None


@router.put("/accounts/{email}")
def edit_account(email: str, body: AccountEditBody, _admin: dict = Depends(require_admin)) -> dict:
    """Edit an account: authority level, name, title, verifier flag, or the email itself."""
    if body.role is not None and body.role not in _ROLES:
        raise HTTPException(400, f"role must be one of {sorted(_ROLES)}")
    try:
        user = appdb.update_account(
            email, role=body.role, name=body.name, title=body.title,
            new_email=body.new_email, verifier=body.verifier)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if user is None:
        raise HTTPException(404, "no such account")
    changed = [k for k, v in (("role", body.role), ("name", body.name),
                              ("title", body.title), ("email", body.new_email),
                              ("verifier", body.verifier)) if v is not None]
    appdb.log_event(_admin["email"], "account", f"edited ({', '.join(changed)})",
                    target=email.strip().lower(),
                    detail=(f"role={body.role}" if body.role is not None else "")
                    + (f" verifier={'on' if body.verifier else 'off'}"
                       if body.verifier is not None else ""))
    return {"ok": True, "user": _public(user)}


class NewAccountBody(BaseModel):
    email: str
    name: str = ""
    title: str = ""
    role: str = "default"
    zone: str = "All"


@router.post("/accounts")
def add_account(body: NewAccountBody, _admin: dict = Depends(require_admin)) -> dict:
    """Add (or update) an account."""
    if "@" not in body.email:
        raise HTTPException(400, "a valid email is required")
    if body.role not in _ROLES:
        raise HTTPException(400, f"role must be one of {sorted(_ROLES)}")
    appdb.upsert_user(body.email, body.name, body.title, body.role, body.zone)
    appdb.log_event(_admin["email"], "account", "added account",
                    target=body.email.strip().lower(), detail=f"role={body.role}")
    return {"ok": True, "email": body.email.strip().lower()}
