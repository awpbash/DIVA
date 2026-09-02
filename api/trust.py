"""api/trust.py — per-document review-trust status, shared across surfaces.

One helper answering "how reviewed is this document?" from the review records +
verification overlays on disk. Consumed by /documents (catalog badge), /chat (citation
trust tags) and the admin dashboard — one definition of "reviewed" everywhere, so the
badge a user sees in chat is exactly the review state the heatmap shows.

Cheap (a handful of small JSON files) but called per chat turn, so results are cached
for a few seconds; a verification lands within one TTL.
"""
from __future__ import annotations

import json
import time

from starlette.concurrency import run_in_threadpool

from pipeline.config import Config

_CFG = Config.load()
_REVIEW = _CFG.storage_root / "review"

_TTL_S = 10.0
_cache: dict = {"at": 0.0, "data": {}}


def _load(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def review_status() -> dict[str, dict]:
    """{doc_id: {populated, verified, tier}} for every document with a review record.

    tier: 'reviewed' (every stated field verified), 'partial' (some), 'unreviewed' (none).
    Documents without a record simply don't appear (treat as unreviewed)."""
    now = time.monotonic()
    if now - _cache["at"] < _TTL_S:
        return _cache["data"]
    out: dict[str, dict] = {}
    if _REVIEW.exists():
        for p in _REVIEW.glob("*.json"):
            if p.name.endswith(".verified.json"):
                continue
            rec = _load(p)
            doc_id = rec.get("doc_id")
            if not doc_id:
                continue
            overlay = _load(_REVIEW / f"{doc_id}.verified.json")
            pop = ver = 0
            for fk, f in (rec.get("fields") or {}).items():
                if f.get("values"):
                    pop += 1
                    if overlay.get(fk, {}).get("verified"):
                        ver += 1
            tier = ("reviewed" if pop and ver == pop
                    else "partial" if ver else "unreviewed")
            out[doc_id] = {"populated": pop, "verified": ver, "tier": tier}
    _cache["at"] = now
    _cache["data"] = out
    return out


async def review_status_async() -> dict[str, dict]:
    """`review_status`, safe to call from an async handler.

    The synchronous version reads two JSON files per document. At sample-corpus
    size that is invisible. At a thousand documents it is two thousand blocking
    reads on the event loop, which stalls every other request and every
    in-flight answer stream for the duration. A cache hit still returns
    immediately without touching the threadpool.
    """
    if time.monotonic() - _cache["at"] < _TTL_S:
        return _cache["data"]
    return await run_in_threadpool(review_status)


def invalidate() -> None:
    """Drop the cache (called after a verification writes, so badges update instantly)."""
    _cache["at"] = 0.0
