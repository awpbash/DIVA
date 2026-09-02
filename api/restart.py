"""api/restart.py — ask this process to restart so freshly-written config
takes effect.

Shared by `/setup/finish`, `/setup/domain/activate` (api/routes/setup.py)
and `/admin/dev-reset` (api/routes/admin.py) — one implementation of "how a
restart actually happens" rather than each caller inventing its own.

Write the config, respond `200`, then on a daemon thread, sleep briefly and
call `os._exit(0)` — a hard, unconditional process exit, no cleanup, no
exec. This relies on whatever is supervising the process to bring a fresh
one up: `docker compose up` (`docker-compose.yml`'s `app` service declares
`restart: unless-stopped`) or `uvicorn --reload` (both the compose dev
command and the plain local-dev invocation), whose own file-watcher often
restarts the worker from the config write alone before the delayed exit
even fires. See docs/setup-wizard-plan.md section 3.6 for the full
reasoning, including why an in-place `os.execvp` (safe in
`api/boot_seed.py`, which runs before uvicorn ever binds a socket) is the
wrong model for restarting a worker that already holds a live listening
socket and connections.
"""
from __future__ import annotations

import os
import threading
import time


def trigger_restart(delay: float = 1.0) -> None:
    """Exit this process after `delay` seconds, on a daemon thread, so the
    caller's own HTTP response reaches the browser first."""
    def _die() -> None:
        time.sleep(delay)
        os._exit(0)
    threading.Thread(target=_die, daemon=True).start()
