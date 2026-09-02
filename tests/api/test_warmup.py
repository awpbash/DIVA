"""Cold-start warmup handling (Cosmos era).

On serverless hosting the api + knowledge store sleep when idle; the first
requests after a wake used to cascade into 500/502s that looked like a crash.
These tests pin the server-side contract:

- Store-unavailable errors map to a retryable 503 ``{"warming": true}``
  signal (the frontend shows a banner and retries on it), never a 500.
- ``/healthz`` stays liveness-only and ``/readyz`` carries the real
  per-dependency readiness, so an orchestrator never kills the container
  just because the store blinked.
"""
from __future__ import annotations

import asyncio
import json

from starlette.requests import Request

from pipeline.store.aio import STORE_UNAVAILABLE_ERRORS


def _req() -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/documents",
        "headers": [], "query_string": b"", "scheme": "http",
        "server": ("test", 80),
    })


def test_store_unavailable_maps_to_warming_503():
    from api.main import _store_warming

    resp = asyncio.run(_store_warming(_req(), STORE_UNAVAILABLE_ERRORS[0]("boot")))
    assert resp.status_code == 503
    assert resp.headers.get("retry-after")
    body = json.loads(resp.body)
    assert body["warming"] is True
    assert "detail" in body


def test_warming_handler_registered_for_all_store_errors():
    from api.main import app, _store_warming

    for exc_cls in STORE_UNAVAILABLE_ERRORS:
        assert app.exception_handlers.get(exc_cls) is _store_warming


def test_healthz_is_liveness_only():
    # No dependency probing here — a downstream blip must never look like a
    # dead process to the orchestrator.
    from api.main import healthz

    resp = asyncio.run(healthz())
    assert resp.status_code == 200
    assert json.loads(resp.body)["ok"] is True


def test_readyz_reports_store_down_as_503(monkeypatch):
    from api import deps, main

    class DeadStore:
        async def verify_connectivity(self):
            raise STORE_UNAVAILABLE_ERRORS[0]("emulator asleep")

    monkeypatch.setattr(deps, "get_store", lambda: DeadStore())
    resp = asyncio.run(main.readyz())
    assert resp.status_code == 503
    body = json.loads(resp.body)
    assert body["ok"] is False
    assert body["cosmos"] == "down"
