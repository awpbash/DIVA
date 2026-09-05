"""Seed-on-empty boot shim (cloud deploys).

The api image starts through this module (Dockerfile CMD). If the storage
volume is empty AND ``STORAGE_SEED_URL`` is set, it downloads the seed
tarball (raw PDFs + pipeline artifacts) onto the volume once, then hands
off to uvicorn unchanged. A fresh volume can therefore always rebuild
itself, no SSH session required.

Local dev never sees this: docker-compose overrides the container command
with its own uvicorn --reload line, and the env var is unset anyway.

Failure policy: a broken seed must not brick the deploy. The app boots
with empty storage (login still works, the doc catalog is just empty) and
the [seed] log lines say exactly what happened.
"""
from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request


def _seed_if_empty() -> None:
    url = os.environ.get("STORAGE_SEED_URL", "").strip()
    root = os.environ.get("STORAGE_ROOT", "storage")
    # The sentinel is written only after a fully successful extraction, so an
    # interrupted seed (deploy restart mid-download/extract) retries on the
    # next boot instead of leaving the volume silently half-seeded (tarfile
    # overwrites existing paths, so a retry over partial content heals).
    # STORAGE_SEED_FORCE=1 ignores the sentinel so a re-stack can refresh an
    # already-seeded volume in place. Unset it again after boot, or every
    # deploy re-downloads the tarball.
    sentinel = os.path.join(root, ".seeded")
    force = os.environ.get("STORAGE_SEED_FORCE", "").strip().lower() in ("1", "true", "yes")
    if not url:
        return
    if os.path.exists(sentinel) and not force:
        print(f"[seed] {sentinel} present — skipping seed", flush=True)
        return
    if force:
        print("[seed] STORAGE_SEED_FORCE set — re-seeding over existing content", flush=True)
    else:
        # No sentinel does NOT mean empty: a failed-then-abandoned seed, or a
        # volume populated by real use, can hold live data (app.db accounts and
        # votes, uploaded PDFs). Pasting the stale tarball over that loses
        # work, so an unforced seed only runs on a genuinely empty volume.
        raw_dir = os.path.join(root, "raw")
        has_content = os.path.exists(os.path.join(root, "app.db")) or (
            os.path.isdir(raw_dir) and len(os.listdir(raw_dir)) > 0)
        if has_content:
            print("[seed] NO sentinel but the volume already has content "
                  "(app.db or raw/*): REFUSING to seed over live data. "
                  "Set STORAGE_SEED_FORCE=1 to override.", flush=True)
            return
    print("[seed] no seed sentinel + STORAGE_SEED_URL set — downloading…", flush=True)
    try:
        os.makedirs(root, exist_ok=True)
        req = urllib.request.Request(
            url,
            # ngrok's free tier serves an interstitial page to browser-like
            # clients unless this header is present, harmless elsewhere.
            headers={"ngrok-skip-browser-warning": "1", "User-Agent": "seed-boot/1"},
        )
        with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
            with urllib.request.urlopen(req, timeout=120) as resp:
                shutil.copyfileobj(resp, tmp)
            path = tmp.name
        size_mb = os.path.getsize(path) / 1e6
        print(f"[seed] downloaded {size_mb:.0f} MB — extracting to {root}…", flush=True)
        with tarfile.open(path) as tf:
            tf.extractall(root, filter="data")
        os.unlink(path)
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write(f"seeded from {url}\n")
        print(f"[seed] done: {sorted(os.listdir(root))}", flush=True)
    except Exception as exc:  # noqa: BLE001 — never brick the boot over seeding
        print(f"[seed] FAILED ({type(exc).__name__}: {exc}) — booting with "
              f"whatever storage holds", flush=True)


def main() -> None:
    _seed_if_empty()
    os.execvp(sys.executable, [
        sys.executable, "-m", "uvicorn", "api.main:app",
        "--host", "0.0.0.0", "--port", "8000",
    ])


if __name__ == "__main__":
    main()
