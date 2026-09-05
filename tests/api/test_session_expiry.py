"""api/appdb.py session lifetime (_SESSION_MAX_AGE) and revocation
(delete_sessions_for). A token used to be valid forever once minted, since
sign-in is passwordless. These guard the 30-day expiry and the admin
sign-out-everywhere path that can now revoke one early.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from api import appdb


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(appdb, "_db_path", lambda: tmp_path / "app.db")
    appdb.init()


def _backdate_session(tmp_path, token: str, age: timedelta) -> None:
    created = (datetime.now(timezone.utc) - age).isoformat()
    with sqlite3.connect(tmp_path / "app.db") as c:
        c.execute("UPDATE sessions SET created_at = ? WHERE token = ?", (created, token))


def test_a_fresh_session_resolves_to_its_user(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    appdb.upsert_user("eng@x.com", "Eng", "", "default", "All")
    token = appdb.create_session("eng@x.com")
    user = appdb.session_user(token)
    assert user is not None
    assert user["email"] == "eng@x.com"


def test_a_session_older_than_the_max_age_is_rejected_and_dropped(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    appdb.upsert_user("eng@x.com", "Eng", "", "default", "All")
    token = appdb.create_session("eng@x.com")
    _backdate_session(tmp_path, token, appdb._SESSION_MAX_AGE + timedelta(days=1))

    assert appdb.session_user(token) is None
    # Not just rejected this once, the stale row is gone from the table.
    with sqlite3.connect(tmp_path / "app.db") as c:
        row = c.execute("SELECT 1 FROM sessions WHERE token = ?", (token,)).fetchone()
    assert row is None


def test_a_session_just_under_the_max_age_still_works(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    appdb.upsert_user("eng@x.com", "Eng", "", "default", "All")
    token = appdb.create_session("eng@x.com")
    _backdate_session(tmp_path, token, appdb._SESSION_MAX_AGE - timedelta(hours=1))
    user = appdb.session_user(token)
    assert user is not None
    assert user["email"] == "eng@x.com"


def test_delete_sessions_for_clears_only_that_email(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    appdb.upsert_user("a@x.com", "A", "", "default", "All")
    appdb.upsert_user("b@x.com", "B", "", "default", "All")
    tok_a1 = appdb.create_session("a@x.com")
    tok_a2 = appdb.create_session("a@x.com")
    tok_b = appdb.create_session("b@x.com")

    cleared = appdb.delete_sessions_for("a@x.com")

    assert cleared == 2
    assert appdb.session_user(tok_a1) is None
    assert appdb.session_user(tok_a2) is None
    assert appdb.session_user(tok_b) is not None  # b's session is untouched


def test_delete_sessions_for_an_email_with_no_sessions_returns_zero(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    appdb.upsert_user("a@x.com", "A", "", "default", "All")
    assert appdb.delete_sessions_for("a@x.com") == 0
