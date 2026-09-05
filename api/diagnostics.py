"""api/diagnostics.py: event-loop stall watchdog.

Symptom this exists to catch: the whole process stops answering EVERY route,
including the dependency-free /healthz, CPU sits near idle, and nothing new
appears in the logs. That combination means one thing: some synchronous call
is running directly on the event loop's own OS thread and never returning (or
taking minutes to). A CPU-bound bug would show high CPU. A dependency outage
would only break the routes that touch that dependency. A total, silent,
idle freeze means the loop itself is stuck.

py-spy / gdb can normally point at the exact frame, but both need ptrace,
which this container's seccomp profile denies (confirmed: py-spy dump fails
with "Permission denied"). This watchdog gets the same answer without ptrace:
it runs on ITS OWN thread (an asyncio-scheduled watchdog would never fire
either, if the loop is the thing that's stuck) and pings the loop with a
trivial callback. If the ping doesn't come back in time, it dumps every
thread's Python stack via sys._current_frames(), pure introspection with no
debugger attach required, so the next occurrence names the exact blocking
call instead of another round of guessing.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback

log = logging.getLogger("api.watchdog")

_CHECK_EVERY = 2.0     # how often the watchdog pings the loop
_STALL_AFTER = 5.0     # a ping unanswered this long counts as stalled
_MIN_LOG_GAP = 30.0    # don't re-dump the same ongoing stall more than this often


def start(loop: asyncio.AbstractEventLoop) -> None:
    """Start the watchdog on a daemon thread. Call once, from lifespan, with
    the loop that's about to serve requests."""
    threading.Thread(target=_watch, args=(loop,), daemon=True,
                      name="event-loop-watchdog").start()
    log.info("event-loop watchdog started (checks every %.0fs, "
              "flags a stall past %.0fs)", _CHECK_EVERY, _STALL_AFTER)


def _watch(loop: asyncio.AbstractEventLoop) -> None:
    last_dump = 0.0
    while True:
        time.sleep(_CHECK_EVERY)
        fired = threading.Event()
        sent_at = time.monotonic()
        try:
            loop.call_soon_threadsafe(fired.set)
        except RuntimeError:
            return  # loop already closed, process is shutting down
        if fired.wait(_STALL_AFTER):
            continue
        now = time.monotonic()
        if now - last_dump < _MIN_LOG_GAP:
            continue  # already dumped this same stall recently; don't spam
        last_dump = now
        log.error(
            "EVENT LOOP STALLED: a trivial callback got no response for over "
            "%.1fs. Every request, including /healthz, is frozen until "
            "whatever is holding the loop's thread returns. Full thread dump "
            "follows so the blocking call is visible without a debugger.",
            now - sent_at)
        for thread_id, frame in sys._current_frames().items():
            stack = "".join(traceback.format_stack(frame))
            log.error("--- thread %s ---\n%s", thread_id, stack)
