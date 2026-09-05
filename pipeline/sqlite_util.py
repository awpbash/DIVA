"""pipeline/sqlite_util.py: a resilient sqlite3 connection, shared by every
small sqlite-backed store in this codebase (api/appdb.py, api/review_votes.py,
pipeline/kb/registry.py, pipeline/kb/ontology_store.py).

WAL mode needs to open and lock two sidecar files (-wal, -shm) alongside the
main database file. When the project directory sits inside a cloud-sync
client's watched folder (OneDrive, Dropbox, Google Drive, common on a managed
work laptop even when the path looks like a plain local folder), that client
can hold a transient lock on one of those files for a moment while it scans
or uploads a change. SQLite surfaces that as a hard
sqlite3.OperationalError("unable to open database file"), not the softer
"database is locked" a busy_timeout smooths over, so it needs its own retry.
A short retry with backoff rides out exactly that kind of transient, external
lock instead of failing the caller's very first statement.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_RETRIES = 10
_BASE_DELAY = 0.1   # seconds, doubles each attempt, capped
_MAX_DELAY = 2.0    # worst case: ~13s total across 10 attempts before giving up


def connect(path: Path, *, timeout: float = 30.0) -> sqlite3.Connection:
    """Open ``path`` with WAL mode and a real busy_timeout, retrying past a
    transient external file lock (see module docstring). Raises the last
    error if every attempt fails, same as a plain sqlite3.connect() would.

    Live-observed on this exact codebase: a real OneDrive lock episode
    outlasted an earlier, shorter version of this retry (5 attempts, under
    2s total), so this now budgets up to ~13s. That is only safe to wait out
    because every caller of this function runs off the request-handling
    thread (a sync route, a sync FastAPI dependency, or an explicit
    asyncio.to_thread from an async one, see api/main.py's _setup_gate for
    why that distinction matters). Calling this directly from an async def
    with no thread offload would tie up the shared event loop for the same
    ~13s worst case instead of just one background thread."""
    last_exc: sqlite3.OperationalError | None = None
    for attempt in range(_RETRIES):
        c = None
        try:
            c = sqlite3.connect(path, timeout=timeout)
            c.execute("PRAGMA journal_mode = WAL")
            c.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
            return c
        except sqlite3.OperationalError as exc:
            if c is not None:
                c.close()
            last_exc = exc
            time.sleep(min(_BASE_DELAY * (2 ** attempt), _MAX_DELAY))
    raise last_exc  # type: ignore[misc]
