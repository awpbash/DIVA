"""pipeline/sqlite_util.py, the resilient sqlite3.connect() wrapper shared by
every small sqlite-backed store in this codebase (api/appdb.py,
api/review_votes.py, pipeline/kb/registry.py, pipeline/kb/ontology_store.py).

Happy-path coverage against a real temp sqlite file: WAL mode and
busy_timeout actually land (not just get asked for), and the returned
connection is usable. The retry-on-OperationalError path (a transient
external file lock, see the module docstring) isn't simulated here, it
needs a real concurrent locker to trigger reliably.
"""
from __future__ import annotations

import sqlite3

from pipeline import sqlite_util


def test_connect_returns_a_working_sqlite_connection(tmp_path):
    conn = sqlite_util.connect(tmp_path / "test.db")
    try:
        assert isinstance(conn, sqlite3.Connection)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO t (name) VALUES (?)", ("hello",))
        conn.commit()
        row = conn.execute("SELECT name FROM t WHERE id = 1").fetchone()
        assert row[0] == "hello"
    finally:
        conn.close()


def test_connect_actually_enables_wal_mode(tmp_path):
    conn = sqlite_util.connect(tmp_path / "test.db")
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_connect_sets_busy_timeout_from_the_timeout_arg(tmp_path):
    conn = sqlite_util.connect(tmp_path / "test.db", timeout=5.0)
    try:
        timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout_ms == 5000
    finally:
        conn.close()


def test_a_second_connection_to_the_same_file_also_gets_wal(tmp_path):
    # WAL is a property of the database file once set, but this also proves
    # a second caller opening the same path doesn't have to fight the first.
    path = tmp_path / "shared.db"
    first = sqlite_util.connect(path)
    try:
        second = sqlite_util.connect(path)
        try:
            mode = second.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            second.close()
    finally:
        first.close()
