"""api/restart.py — ask this process to restart so freshly-written config
takes effect.

Shared by `/setup/finish`, `/setup/domain/activate` (api/routes/setup.py)
and `/admin/dev-reset` (api/routes/admin.py) — one implementation of "how a
restart actually happens" rather than each caller inventing its own.

Write the config, respond `200`, then on a daemon thread, sleep briefly and
exit. Confirmed live (docker inspect + /proc) under `uvicorn --reload` (the
compose dev command): PID 1 in the container is the reload supervisor, and
this handler runs in its worker *child* process. Exiting only the child
leaves the supervisor alive with nothing to respawn until it next notices a
watched-file change on its own schedule — a race that was lost during real
testing, leaving the instance unreachable until someone found it and ran
`docker compose restart` by hand. So: if we're not PID 1, SIGTERM our parent
(the supervisor) first — that takes the whole container down, which is what
`docker-compose.yml`'s `restart: unless-stopped` actually reacts to — then
exit ourselves either way (the plain `uvicorn` invocation with no --reload
has no separate supervisor, so self-exit alone is already correct there).
See docs/setup-wizard-plan.md section 3.6 for why an in-place `os.execvp`
(safe in `api/boot_seed.py`, which runs before uvicorn ever binds a socket)
is the wrong model for restarting a worker that already holds a live
listening socket and connections.
"""
from __future__ import annotations

import os
import signal
import threading
import time


def trigger_restart(delay: float = 1.0) -> None:
    """Exit this process (and its reload supervisor, if any) after `delay`
    seconds, on a daemon thread, so the caller's own HTTP response reaches
    the browser first."""
    def _die() -> None:
        time.sleep(delay)
        if os.getpid() != 1:
            try:
                os.kill(os.getppid(), signal.SIGTERM)
            except OSError:
                pass
        os._exit(0)
    threading.Thread(target=_die, daemon=True).start()
