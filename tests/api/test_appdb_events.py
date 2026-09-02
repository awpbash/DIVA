"""appdb activity log + feedback attachments — storage-level behavior.

The activity feed and screenshot attachments ride on two appdb additions:
an append-only ``events`` table (audited mutations) and an ``attachments``
JSON column on feedback. Both must migrate old databases in place.
"""
from __future__ import annotations

import json
import sqlite3

from api import appdb


def _old_db(tmp_path):
    """A database from before events/attachments existed."""
    db = tmp_path / "app.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE users (email TEXT PRIMARY KEY, name TEXT, "
                  "title TEXT, role TEXT NOT NULL, zone TEXT, "
                  "verifier INTEGER NOT NULL DEFAULT 0)")
        c.execute("INSERT INTO users VALUES('eng@x.com','Eng','','default','All',0)")
        c.execute("CREATE TABLE feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "created_at TEXT NOT NULL, email TEXT NOT NULL, category TEXT NOT NULL, "
                  "message TEXT NOT NULL, context TEXT, status TEXT NOT NULL DEFAULT 'new', "
                  "status_by TEXT, status_at TEXT)")
    return db


def test_feedback_attachments_column_migration(tmp_path, monkeypatch):
    db = _old_db(tmp_path)
    monkeypatch.setattr(appdb, "_db_path", lambda: db)
    appdb.init()                                       # migrates + is re-runnable
    appdb.init()
    fid = appdb.add_feedback("eng@x.com", "other", "see screenshot",
                             attachments_json=json.dumps(["a" * 32 + ".png"]))
    got = next(i for i in appdb.list_feedback() if i["id"] == fid)
    assert json.loads(got["attachments"]) == ["a" * 32 + ".png"]
    # Reports without screenshots keep a NULL column.
    fid2 = appdb.add_feedback("eng@x.com", "other", "no shot")
    assert next(i for i in appdb.list_feedback() if i["id"] == fid2)["attachments"] is None


def test_events_log_roundtrip_and_caps(tmp_path, monkeypatch):
    db = _old_db(tmp_path)
    monkeypatch.setattr(appdb, "_db_path", lambda: db)
    appdb.init()
    appdb.log_event("Adm@X.com", "ontology", "added field", target="cat.key", detail="Title")
    appdb.log_event("adm@x.com", "bogus-kind", "x" * 500, target="t" * 500, detail="d" * 5000)
    events = appdb.list_events()
    assert len(events) == 2
    assert events[0]["action"] == "x" * 80              # newest first + truncated
    assert events[0]["kind"] == "other"                 # unknown kinds normalised
    assert len(events[0]["target"]) == 200
    assert len(events[0]["detail"]) == 500
    assert events[1]["actor"] == "adm@x.com"            # lowercased


def test_log_event_never_raises(tmp_path, monkeypatch):
    # A broken database must not break the mutation being audited.
    monkeypatch.setattr(appdb, "_db_path", lambda: tmp_path / "missing" / "app.db")
    appdb.log_event("a@x.com", "account", "edited")     # no table, no dir — swallowed


def test_usage_rollup(tmp_path, monkeypatch):
    db = _old_db(tmp_path)
    monkeypatch.setattr(appdb, "_db_path", lambda: db)
    appdb.init()
    appdb.log_usage("a@x.com", "login")
    appdb.log_usage("a@x.com", "chat", prompt_tokens=1000, completion_tokens=200, calls=5)
    appdb.log_usage("a@x.com", "chat", prompt_tokens=500, completion_tokens=100, calls=3)
    appdb.log_usage("b@x.com", "login")
    appdb.log_usage("b@x.com", "login")

    rows = {r["email"]: r for r in appdb.usage_by_user()}
    a, b = rows["a@x.com"], rows["b@x.com"]
    assert a["questions"] == 2 and a["logins"] == 1
    assert a["prompt_tokens"] == 1500 and a["completion_tokens"] == 300
    assert a["last_active"] is not None
    assert b["questions"] == 0 and b["logins"] == 2 and b["last_active"] is None
    # Heaviest token user first.
    assert appdb.usage_by_user()[0]["email"] == "a@x.com"

    daily = appdb.usage_daily(14)
    assert len(daily) == 1                              # everything logged today
    assert daily[0]["questions"] == 2 and daily[0]["logins"] == 3
    assert daily[0]["tokens"] == 1800

    by_user = appdb.usage_daily_by_user(7)
    assert len(by_user) == 1                            # only chat rows, only a@
    assert by_user[0]["email"] == "a@x.com"
    assert by_user[0]["questions"] == 2 and by_user[0]["tokens"] == 1800


def test_log_usage_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(appdb, "_db_path", lambda: tmp_path / "missing" / "app.db")
    appdb.log_usage("a@x.com", "chat", prompt_tokens=1)  # no table, no dir — swallowed


# --------------------------------------------------------------------------- #
# The first-run sign-in hint. Sign-in is passwordless, so an email address IS a
# credential: naming one on a public login screen has to be exactly bounded.
# --------------------------------------------------------------------------- #
def _fresh(tmp_path, monkeypatch, email="admin@localhost"):
    monkeypatch.setattr(appdb, "_db_path", lambda: tmp_path / "app.db")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", email)
    appdb.init()          # creates the bootstrap admin, there being no accounts


def test_a_fresh_instance_names_the_account_it_created(tmp_path, monkeypatch):
    """Otherwise the operator of a new install has a login screen, one account,
    and no way to learn its address."""
    _fresh(tmp_path, monkeypatch, "owner@example.org")
    assert appdb.first_run_email() == "owner@example.org"


def test_the_hint_stops_after_the_first_sign_in(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    appdb.log_usage("admin@localhost", "login")
    assert appdb.first_run_email() is None


def test_a_sign_in_by_anyone_stops_the_hint(tmp_path, monkeypatch):
    """The bar is 'this instance has never been used', not 'this account has
    never signed in'. A used instance never advertises an address again."""
    _fresh(tmp_path, monkeypatch)
    appdb.log_usage("someone.else@example.org", "login")
    assert appdb.first_run_email() is None


def test_no_hint_once_a_second_account_exists(tmp_path, monkeypatch):
    """More than one account means somebody has administered this instance, so
    it is not a fresh install and we are not guessing which address is safe."""
    _fresh(tmp_path, monkeypatch)
    appdb.upsert_user("second@example.org", "Second", "", "default", "All")
    assert appdb.first_run_email() is None


def test_other_usage_kinds_do_not_count_as_a_sign_in(tmp_path, monkeypatch):
    """Only a login retires the hint. A chat row cannot exist before one, but
    the rule should be about sign-ins rather than about activity in general."""
    _fresh(tmp_path, monkeypatch)
    appdb.log_usage("admin@localhost", "chat", prompt_tokens=10)
    assert appdb.first_run_email() == "admin@localhost"
