"""scripts/_session.py — a login session for headless clients.

/chat and friends are login-gated now, so every headless caller needs an
X-User-Token. Resolution order:

  1. env CHAT_TOKEN            — an existing session token, used as-is
  2. env CHAT_EMAIL            — log in as that account
  3. BOOTSTRAP_ADMIN_EMAIL     — the instance's first admin, which `scripts.setup`
                                 creates and `.env` names (some headless jobs
                                 need admin: role impersonation on /chat is
                                 admin-only)

Step 3 used to read the account list off the server. That endpoint is admin-only
now, and correctly so: under passwordless sign-in an email address IS the
credential, so an unauthenticated caller must not be able to enumerate them.

Passwordless demo auth, so "logging in" is one POST. The token is cached per
process; a real credential flow would slot in here without touching callers.
"""
from __future__ import annotations

import os

import httpx

_TOKEN: str | None = None


def resolve_token(base: str = "http://localhost:8000") -> str:
    global _TOKEN
    if _TOKEN:
        return _TOKEN
    env_token = os.environ.get("CHAT_TOKEN", "").strip()
    if env_token:
        _TOKEN = env_token
        return _TOKEN

    email = (os.environ.get("CHAT_EMAIL", "").strip()
             or os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "").strip()
             or "admin@localhost")

    r = httpx.post(f"{base}/auth/login", json={"email": email}, timeout=20)
    if r.status_code == 401:
        raise RuntimeError(
            f"no account {email!r} on this instance — set CHAT_EMAIL to one of "
            "yours, or CHAT_TOKEN to a session token")
    r.raise_for_status()
    _TOKEN = r.json()["token"]
    return _TOKEN


def auth_headers(base: str = "http://localhost:8000") -> dict[str, str]:
    return {"X-User-Token": resolve_token(base)}
