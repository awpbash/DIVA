"""api/restart.py — ask this process to restart so freshly-written config
takes effect.

Shared by `/setup/finish`, `/setup/domain/activate` (api/routes/setup.py)
and `/admin/dev-reset` (api/routes/admin.py) — one implementation of "how a
restart actually happens" rather than each caller inventing its own.

Write the config, respond `200`, then on a daemon thread, sleep briefly and
exit. Two real setups, two different mechanisms:

  - No `--reload` (`docker-compose*.yml`'s prod invocation, via
    `api/boot_seed.py`'s `os.execvp` into plain uvicorn): this process IS
    PID 1, so exiting it is what `restart: always`/`unless-stopped` reacts
    to. Unchanged from the original, always-worked version of this file.
  - `--reload` (both compose files' dev command, and a developer running
    `uvicorn --reload` directly): confirmed live via `docker inspect` +
    `/proc` that PID 1 is uvicorn's reload supervisor and this handler runs
    in its worker *child*. Exiting only the child left the supervisor alive
    with nothing to respawn it — the file-watcher restart the old comment
    here assumed only ever applied to `/setup/domain/activate` (the one
    caller that writes a watched-dir file in the same request); `finish()`
    and `dev-reset()` write to `storage/app.db`, which isn't watched, so
    they never had a safety net. The container sat unreachable until
    someone found it and ran `docker compose restart` by hand.
    Under Docker, killing the worker's parent (the supervisor, i.e. the
    container's PID 1) is safe: `restart: always`/`unless-stopped` brings
    the whole thing back. Outside Docker there is no such policy, so doing
    the same thing would just kill the developer's terminal session dead
    with no recovery — worse than today's occasional silent hang. `/.dockerenv`
    is Docker's own marker file, present in every container it starts, and
    is the cheap way to tell those two cases apart.

`api/boot_seed.py` uses an in-place `os.execvp` for its own restart-into-uvicorn
step, which only works because it runs BEFORE uvicorn ever binds a socket: a
clean handoff from one process image to the next with nothing live yet.
Calling `os.execvp` from inside a running request handler instead is a
different, riskier thing: it replaces the process image of a worker that
already holds an open listening socket and live connections, and getting the
new image to either reuse or cleanly release that socket before rebinding is
exactly the kind of fd-level edge case not worth risking here. A hard exit
and letting the supervisor bring up a genuinely fresh process, as this module
does, sidesteps that entirely.
"""
from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

_IN_CONTAINER = Path("/.dockerenv").exists()


def trigger_restart(delay: float = 1.0) -> None:
    """Exit this process (and its reload supervisor, if any and if it's
    safe to) after `delay` seconds, on a daemon thread, so the caller's own
    HTTP response reaches the browser first."""
    def _die() -> None:
        time.sleep(delay)
        if _IN_CONTAINER and os.getpid() != 1:
            try:
                os.kill(os.getppid(), signal.SIGTERM)
            except OSError:
                pass
        os._exit(0)
    threading.Thread(target=_die, daemon=True).start()
