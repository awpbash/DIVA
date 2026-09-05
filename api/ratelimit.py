"""api/ratelimit.py: a small in-memory rate limiter.

Single-process by design, matching every other in-memory store in this
codebase (api/routes/admin.py's _JOBS, api/rag/policy.py's _ACTIVE): state
resets on restart, which is the right tradeoff for a demo-scale single
container and the wrong one for a horizontally-scaled deployment, where a
real limiter needs shared state (Redis, a proxy) instead of one process's
memory. Good enough to close the two gaps this app actually had: sign-in
being passwordless made an unthrottled /auth/login a free account-email
scanner, and /chat having no per-account ceiling meant one account could
run up the OpenAI bill with nothing to stop it.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

_LOCK = threading.Lock()
_HITS: dict[str, deque[float]] = defaultdict(deque)


def reset() -> None:
    """Clear all standing counts. Called on every app startup (see
    api/main.py's lifespan): a fresh process has made zero calls so far, same
    as a real restart would give you for free, and it keeps this state from
    leaking between tests that each spin up their own TestClient(app) in the
    same pytest process."""
    _HITS.clear()


def allow(key: str, *, limit: int, window: float) -> bool:
    """True if ``key`` has made fewer than ``limit`` calls in the trailing
    ``window`` seconds, and records this call as one of them if so. False
    means the caller should reject the request, and the count for ``key``
    is left unchanged (a rejected attempt does not buy itself more rope)."""
    now = time.monotonic()
    with _LOCK:
        hits = _HITS[key]
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


def client_key(request) -> str:
    """Best-effort caller identity for throttling. Trusts X-Forwarded-For's
    first hop when present (the standard deployment here sits behind a
    reverse proxy, see docs/DEPLOYMENT.md), falling back to the direct
    socket address for a bare `docker compose up`. Spoofable by a caller
    that talks to this app directly with no proxy in front, which only
    weakens the limiter back to "trust the client's own claimed address",
    never below that."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
