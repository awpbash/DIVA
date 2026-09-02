"""api/appdb.py — the application state store (accounts, sessions, per-user chat history).

A small local SQLite file (storage/app.db) that holds the app's "who / what / status"
layer, distinct from the KNOWLEDGE store. Deliberately thin: stdlib sqlite3, one
connection per call (cheap for a local file, no pooling ceremony). Login resolves an
email to its authority level, that level drives RBAC across the app, and each user's
conversations persist server-side.

Bootstrapping: a fresh install has no accounts, so the first boot creates a single
admin from ``BOOTSTRAP_ADMIN_EMAIL`` (default ``admin@localhost``). That account then
invites everyone else from the admin dashboard. Nothing is read from a spreadsheet.

Demo-grade: passwordless (email identifies the account). Real auth (password / SSO) is a
contained swap later — the access RULES already live in the ontology sensitivity + the
review/km RBAC and don't change. Say so out loud before putting this on the open
internet: anyone who knows an account's email address can sign in as that account.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config import Config

_ROLES = {"admin", "confidential", "default"}
_DEFAULT_ROLE = "default"


def _db_path() -> Path:
    return Config.load().storage_root / "app.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path())
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    email    TEXT PRIMARY KEY,
    name     TEXT,
    title    TEXT,
    role     TEXT NOT NULL,
    zone     TEXT,
    verifier INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS thread_store (
    email      TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    email       TEXT NOT NULL,
    category    TEXT NOT NULL,
    message     TEXT NOT NULL,
    context     TEXT,
    status      TEXT NOT NULL DEFAULT 'new',
    status_by   TEXT,
    status_at   TEXT,
    attachments TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    actor  TEXT NOT NULL,
    kind   TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS usage_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    at                TEXT NOT NULL,
    email             TEXT NOT NULL,
    kind              TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    calls             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_email ON usage_log(email);
CREATE TABLE IF NOT EXISTS jobs (
    doc_id     TEXT PRIMARY KEY,
    kind       TEXT,
    status     TEXT NOT NULL,
    stage      TEXT,
    error      TEXT,
    started_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------- #
# Ingestion jobs — persisted status so a container restart can't lose them.
# The in-memory registry in api/routes/admin.py remains the hot path; these
# rows are the durable record behind /admin/jobs, interrupted-job detection
# at boot, and the one-click retry.
# --------------------------------------------------------------------------- #


def job_upsert(doc_id: str, **fields) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE doc_id = ?", (doc_id,)).fetchone()
        merged = {**(dict(row) if row else {}), **fields}
        c.execute(
            "INSERT INTO jobs (doc_id, kind, status, stage, error, started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(doc_id) DO UPDATE SET kind=excluded.kind, "
            "status=excluded.status, stage=excluded.stage, error=excluded.error, "
            "started_at=excluded.started_at, updated_at=excluded.updated_at",
            (doc_id, merged.get("kind"), merged.get("status") or "unknown",
             merged.get("stage"), merged.get("error"),
             merged.get("started_at") or (now if fields.get("status") == "running" else None),
             now))


def job_rows() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM jobs ORDER BY updated_at DESC")]


def jobs_mark_interrupted() -> int:
    """Boot-time sweep: any job still 'running' did not survive the last
    process. Mark it interrupted so the admin sees a clear retry prompt
    instead of a stuck spinner. The pipeline caches every stage, so a retry
    resumes from where it stopped without re-spending tokens."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _conn() as c:
        cur = c.execute(
            "UPDATE jobs SET status = 'interrupted', "
            "stage = 'interrupted by a restart — click Extract to resume', "
            "updated_at = ? WHERE status = 'running'", (now,))
        return cur.rowcount


def init() -> None:
    """Create tables if absent, then bootstrap one admin account if there are none."""
    with _conn() as c:
        c.executescript(_SCHEMA)
        # Idempotent column migrations for databases created before these
        # features existed (CREATE IF NOT EXISTS won't alter an old table).
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "verifier" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN verifier INTEGER NOT NULL DEFAULT 0")
        fb_cols = {r["name"] for r in c.execute("PRAGMA table_info(feedback)")}
        if "attachments" not in fb_cols:
            c.execute("ALTER TABLE feedback ADD COLUMN attachments TEXT")
    if not list_users():
        bootstrap_admin()


def default_bootstrap_email() -> str:
    """The email `bootstrap_admin` creates (or already created). One place, so
    the setup wizard's "has the default account been retired?" check reads
    the exact same rule `bootstrap_admin` uses to create it, rather than a
    second hardcoded copy of "admin@localhost" that could drift from it."""
    return (os.getenv("BOOTSTRAP_ADMIN_EMAIL") or "admin@localhost").strip().lower()


def bootstrap_admin() -> str:
    """Create the first admin account so a fresh install is not locked out of every
    admin-gated surface. Returns the email. Only ever called when the table is empty,
    so it cannot demote or overwrite a real account later."""
    email = default_bootstrap_email()
    name = os.getenv("BOOTSTRAP_ADMIN_NAME") or "Administrator"
    upsert_user(email, name, "Bootstrap account", "admin", "All")
    update_account(email, verifier=True)   # so the Review tab works out of the box
    return email


def first_run_email() -> str | None:
    """The address to sign in with on a brand-new instance, or None.

    Sign-in is passwordless, so an email address IS a credential and the login
    screen must not enumerate accounts. But a fresh install has exactly one
    account, created by `bootstrap_admin` from a setting the operator either
    chose or defaulted, and there is no way for them to learn it from the
    screen. That is a locked door with the key in the documentation.

    So: name it, and only while it is genuinely a fresh install. One account,
    which has never signed in. The first successful sign-in writes a `login`
    row and this returns None forever after, on every instance that has ever
    been used by anyone.
    """
    users = list_users()
    if len(users) != 1:
        return None
    email = str(users[0].get("email") or "")
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM usage_log WHERE kind = 'login' LIMIT 1").fetchone()
    return None if row else (email or None)


# --------------------------------------------------------------------------- #
# Settings (small key/value store — currently just the setup-wizard flag)
# --------------------------------------------------------------------------- #
def get_setting(key: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))


_SETUP_COMPLETE_KEY = "setup_complete"
_SETUP_PENDING_KEY = "setup_pending"


def is_setup_complete() -> bool:
    """True once the wizard (or an equivalent by-hand .env setup) has
    finished. Three checks, in order:

      1. explicit — `mark_setup_complete` wrote the flag (wizard finished).
      2. pending override — `mark_setup_pending` wrote ITS flag, which
         forces this to False regardless of #3 below. Set by a reset (so a
         wiped instance can't look "done" again just because its domain
         autodetects, its admin regrows on boot, and the key was kept — see
         scripts/reset_dev.py) and by the wizard's own guard the moment any
         `/setup/*` mutation runs (so finishing the admin step mid-wizard
         can't self-complete before the remaining steps happen).
      3. inferred — a domain resolves, an admin exists, and a real
         (non-placeholder) API key is configured. What a hand-configured
         `scripts/setup.py` deployment already looks like, so it never has
         to open the wizard. (Requiring a NON-default admin here instead was
         tried and rejected: it would 503 every dev/test boot that never
         renames admin@localhost. That rule is instead a hard check in the
         wizard's own `/setup/finish`, where a human can act on it.)
    """
    if get_setting(_SETUP_COMPLETE_KEY) == "1":
        return True
    if get_setting(_SETUP_PENDING_KEY) == "1":
        return False
    from pipeline import ontology
    if not ontology.DEFAULT_DOCTYPE:
        return False
    if not any(u["role"] == "admin" for u in list_users()):
        return False
    key = os.getenv("OPENAI_API_KEY", "").strip()
    return bool(key) and not key.startswith("sk-...")


def mark_setup_complete() -> None:
    set_setting(_SETUP_COMPLETE_KEY, "1")


def mark_setup_pending() -> None:
    """See is_setup_complete: forces the inferred-completion branch off
    until the next mark_setup_complete()."""
    if get_setting(_SETUP_PENDING_KEY) != "1":
        set_setting(_SETUP_PENDING_KEY, "1")


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
def _norm_role(v: str | None) -> str:
    r = str(v or "").strip().lower()
    return r if r in _ROLES else _DEFAULT_ROLE


def upsert_user(email: str, name: str, title: str, role: str, zone: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO users(email, name, title, role, zone) VALUES(?,?,?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET name=excluded.name, title=excluded.title, "
            "role=excluded.role, zone=excluded.zone",
            (email.strip().lower(), name, title, _norm_role(role), zone))


def get_user(email: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM users ORDER BY role, name").fetchall()
    return [dict(r) for r in rows]


def set_role(email: str, role: str) -> bool:
    """Change an account's authority level (admin action). Returns False if no such user."""
    with _conn() as c:
        cur = c.execute("UPDATE users SET role = ? WHERE email = ?",
                        (_norm_role(role), email.strip().lower()))
        return cur.rowcount > 0


def delete_user(email: str) -> bool:
    """Remove an account plus its live sessions and cached chat threads. Returns False
    if there was no such account. The audit trail (events, usage, feedback, review
    votes) keeps the email as historical text: an engineering-contract KB has to be
    able to say who verified what, and deleting the account must not rewrite that."""
    e = email.strip().lower()
    with _conn() as c:
        if not c.execute("SELECT 1 FROM users WHERE email = ?", (e,)).fetchone():
            return False
        c.execute("DELETE FROM sessions WHERE email = ?", (e,))
        c.execute("DELETE FROM thread_store WHERE email = ?", (e,))
        c.execute("DELETE FROM users WHERE email = ?", (e,))
    return True


def update_account(email: str, *, name: str | None = None, title: str | None = None,
                   role: str | None = None, new_email: str | None = None,
                   verifier: bool | None = None) -> dict | None:
    """Edit an account (admin action). Only the fields passed change. Renaming the
    email migrates the account's sessions and chat history with it — the email is the
    key everywhere, so the person keeps their login and conversations. Returns the
    updated user, None if no such account. Raises ValueError on a bad/taken email."""
    old = email.strip().lower()
    tgt = (new_email or "").strip().lower() or old
    if tgt != old and "@" not in tgt:
        raise ValueError("New email must contain @")
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (old,)).fetchone()
        if not row:
            return None
        if tgt != old and c.execute("SELECT 1 FROM users WHERE email = ?", (tgt,)).fetchone():
            raise ValueError(f"{tgt} already has an account")
        c.execute(
            "UPDATE users SET email=?, name=?, title=?, role=?, verifier=? WHERE email=?",
            (tgt,
             row["name"] if name is None else name.strip(),
             row["title"] if title is None else title.strip(),
             row["role"] if role is None else _norm_role(role),
             row["verifier"] if verifier is None else int(bool(verifier)),
             old))
        if tgt != old:
            c.execute("UPDATE sessions SET email=? WHERE email=?", (tgt, old))
            c.execute("DELETE FROM thread_store WHERE email=?", (tgt,))   # clear any orphan
            c.execute("UPDATE thread_store SET email=? WHERE email=?", (tgt, old))
    return get_user(tgt)


# --------------------------------------------------------------------------- #
# Sessions (passwordless — a login mints a random opaque token)
# --------------------------------------------------------------------------- #
def create_session(email: str) -> str:
    token = secrets.token_urlsafe(24)
    with _conn() as c:
        c.execute("INSERT INTO sessions(token, email, created_at) VALUES(?,?,?)",
                  (token, email.strip().lower(), _now()))
    return token


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.email = s.email WHERE s.token = ?",
            (token,)).fetchone()
    return dict(row) if row else None


def delete_session(token: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


# --------------------------------------------------------------------------- #
# User feedback (in-app issue reports; triaged in the admin dashboard)
# --------------------------------------------------------------------------- #
_FEEDBACK_CATEGORIES = {"ui_ux", "extraction", "answer", "other"}
_FEEDBACK_STATUSES = {"new", "acknowledged", "fixed"}


def add_feedback(email: str, category: str, message: str,
                 context_json: str | None = None,
                 attachments_json: str | None = None) -> int:
    """Store one feedback item. Category/size are normalized server-side —
    the client is never trusted with identity or unbounded blobs.
    `attachments_json` is a JSON list of server-stored screenshot names
    (validated by the route, never client paths)."""
    cat = str(category or "").strip().lower()
    if cat not in _FEEDBACK_CATEGORIES:
        cat = "other"
    msg = str(message or "").strip()[:4000]
    ctx = context_json[:16_384] if context_json else None
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO feedback(created_at, email, category, message, context, attachments) "
            "VALUES(?,?,?,?,?,?)",
            (_now(), email.strip().lower(), cat, msg, ctx, attachments_json))
        return int(cur.lastrowid)


def list_feedback(status: str | None = None) -> list[dict]:
    """Newest first; optionally filtered by triage status."""
    q = "SELECT * FROM feedback"
    args: tuple = ()
    if status and status in _FEEDBACK_STATUSES:
        q += " WHERE status = ?"
        args = (status,)
    q += " ORDER BY id DESC"
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
    return [dict(r) for r in rows]


def set_feedback_status(fid: int, status: str, by: str) -> bool:
    """Triage transition (admin action). Returns False on unknown id/status."""
    if status not in _FEEDBACK_STATUSES:
        return False
    with _conn() as c:
        cur = c.execute(
            "UPDATE feedback SET status=?, status_by=?, status_at=? WHERE id=?",
            (status, by.strip().lower(), _now(), int(fid)))
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Activity log (the admin changelog: who did what, when)
# --------------------------------------------------------------------------- #
_EVENT_KINDS = {"ontology", "account", "document", "feedback"}


def log_event(actor: str, kind: str, action: str,
              target: str = "", detail: str = "") -> None:
    """Append one audit event. Never raises — an audit write must not be able
    to fail the mutation it describes. Verification votes are NOT logged here
    (field_votes already keeps their complete history); the activity feed
    merges both at read time."""
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO events(at, actor, kind, action, target, detail) "
                "VALUES(?,?,?,?,?,?)",
                (_now(), str(actor or "").strip().lower(),
                 kind if kind in _EVENT_KINDS else "other",
                 str(action or "")[:80], str(target or "")[:200],
                 str(detail or "")[:500]))
    except Exception:  # noqa: BLE001 — best-effort audit trail
        pass


def list_events(limit: int = 200) -> list[dict]:
    """Newest first."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?",
                         (max(1, min(int(limit), 1000)),)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Usage metering (who uses the app, how much: logins + per-answer tokens)
# --------------------------------------------------------------------------- #
def log_usage(email: str, kind: str, prompt_tokens: int = 0,
              completion_tokens: int = 0, calls: int = 0) -> None:
    """Append one usage row: ``kind='login'`` marks a sign-in, ``kind='chat'``
    carries one answer's exact token spend (from the request's TokenMeter).
    Append-only on purpose — the sessions table loses rows on logout, so it
    cannot serve as a login history. Never raises: metering must not be able
    to fail the chat or login it measures."""
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO usage_log(at, email, kind, prompt_tokens, completion_tokens, calls) "
                "VALUES(?,?,?,?,?,?)",
                (_now(), str(email or "").strip().lower(), kind,
                 int(prompt_tokens), int(completion_tokens), int(calls)))
    except Exception:  # noqa: BLE001 — best-effort metering
        pass


def usage_by_user() -> list[dict]:
    """Per-account rollup: questions asked, exact token spend, logins, and the
    last time they signed in / asked something. Heaviest token users first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT email, "
            "  SUM(CASE WHEN kind = 'chat' THEN 1 ELSE 0 END)  AS questions, "
            "  SUM(prompt_tokens)                              AS prompt_tokens, "
            "  SUM(completion_tokens)                          AS completion_tokens, "
            "  SUM(CASE WHEN kind = 'login' THEN 1 ELSE 0 END) AS logins, "
            "  MAX(CASE WHEN kind = 'login' THEN at END)       AS last_login, "
            "  MAX(CASE WHEN kind = 'chat' THEN at END)        AS last_active "
            "FROM usage_log GROUP BY email "
            "ORDER BY prompt_tokens + completion_tokens DESC, questions DESC",
        ).fetchall()
    return [dict(r) for r in rows]


def usage_daily(days: int = 14) -> list[dict]:
    """Questions + tokens per day (UTC) for the recent-activity strip.
    Sparse: days with no rows are absent — chart callers zero-fill."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT substr(at, 1, 10) AS day, "
            "  SUM(CASE WHEN kind = 'chat' THEN 1 ELSE 0 END)  AS questions, "
            "  SUM(prompt_tokens + completion_tokens)          AS tokens, "
            "  SUM(CASE WHEN kind = 'login' THEN 1 ELSE 0 END) AS logins "
            "FROM usage_log WHERE at >= ? GROUP BY day ORDER BY day",
            (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def usage_daily_by_user(days: int = 7) -> list[dict]:
    """Per-user questions + tokens per day (UTC) over the trailing window
    (today plus the previous ``days - 1``). Sparse like usage_daily —
    the metrics route aligns every user onto one dense day axis."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=max(1, days) - 1)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT email, substr(at, 1, 10) AS day, "
            "  SUM(CASE WHEN kind = 'chat' THEN 1 ELSE 0 END) AS questions, "
            "  SUM(prompt_tokens + completion_tokens)         AS tokens "
            "FROM usage_log WHERE kind = 'chat' AND at >= ? "
            "GROUP BY email, day ORDER BY email, day",
            (cutoff,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Per-user chat history (the whole thread blob, keyed by account)
# --------------------------------------------------------------------------- #
def get_threads(email: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT data FROM thread_store WHERE email = ?",
                        (email.strip().lower(),)).fetchone()
    return row["data"] if row else None


def put_threads(email: str, data: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO thread_store(email, data, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (email.strip().lower(), data, _now()))
