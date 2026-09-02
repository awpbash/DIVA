"""pipeline/filestore.py — the file-storage seam (local folder / Azure Blob).

The pipeline and API keep reading and writing plain files under
``cfg.storage_root`` — fast, simple, and identical in dev and prod. This
module makes that directory DURABLE on Azure: Blob Storage holds the mirror,
the local disk is the working cache.

  * dev (no Blob configured)  → every function is a silent no-op
  * prod (Container Apps)     → ``pull()`` at boot restores the cache from
    Blob; ``push()`` after any write (upload, extract job, review edit)
    persists the changed prefixes back

Configuration (either form):
  AZURE_STORAGE_CONNECTION_STRING   connection string (key-based)
  AZURE_BLOB_ACCOUNT_URL            https://<acct>.blob.core.windows.net
                                    (uses DefaultAzureCredential — managed
                                    identity on Container Apps)
  AZURE_BLOB_CONTAINER              container name (default: storage)

Sync semantics are deliberately simple: compare size, copy when different or
missing. Contents are content-addressed (doc_id = file hash) or append-ish
(review overlays, sidecars), so size-compare is a safe cheap proxy at this
corpus scale. The SQLite app database is NOT mirrored here — it lives on the
mounted volume (see the deployment notes).
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path

from .config import Config
from .storage import FIELDS_DIR

log = logging.getLogger("store.files")

# The storage/ prefixes worth mirroring: everything a rebuild or the UI
# needs. emb_cache is included so a fresh container never re-spends
# embedding tokens. tmp/, migration/ and the app db are deliberately out.
# Both field-extraction directory names are listed: an install created before
# the rename still has the legacy one, and dropping it from the mirror would
# quietly stop backing up every extraction it holds.
PREFIXES = (
    "raw", "doc", "doc_geometry", "pages_md", "canonical", "harvest",
    FIELDS_DIR, "review", "vocab", "emb_cache", "assets",
)


def enabled() -> bool:
    return bool(os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
                or os.environ.get("AZURE_BLOB_ACCOUNT_URL"))


def _container():
    from azure.storage.blob import ContainerClient
    name = os.environ.get("AZURE_BLOB_CONTAINER", "storage")
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn:
        client = ContainerClient.from_connection_string(conn, name)
    else:
        from azure.identity import DefaultAzureCredential
        client = ContainerClient(
            account_url=os.environ["AZURE_BLOB_ACCOUNT_URL"],
            container_name=name, credential=DefaultAzureCredential())
    try:
        client.create_container()
    except Exception:  # noqa: BLE001 — already exists (the normal case)
        pass
    return client


def _same_content(local: Path, blob) -> bool:
    """Whether a local file and its blob hold the same bytes.

    Compares the CONTENT, via the MD5 Azure stores alongside every blob, and
    falls back to size only when the blob has no MD5 recorded.

    Size alone was the old test and it is wrong for exactly the files that
    matter most. A verifier correcting a value to a same-length string, a
    second vote flipping `n_votes` from 1 to 2, a confidence moving 0.5 to 1.0:
    each rewrites `<doc>.verified.json` at an identical byte count, so the
    upload was skipped and the next container recreation restored the old
    file over it. That is the human verification layer, which is the most
    valuable data here and the one thing that cannot be recomputed.
    """
    stat = local.stat()
    try:
        remote_md5 = (blob.content_settings or {}).get("content_md5")
    except (AttributeError, TypeError):
        remote_md5 = None
    if remote_md5:
        digest = hashlib.md5(local.read_bytes()).digest()  # noqa: S324 (not security)
        return bytes(remote_md5) == digest
    return stat.st_size == (getattr(blob, "size", None) or 0)


def pull(cfg: Config, prefixes: tuple[str, ...] = PREFIXES) -> int:
    """Blob → local. Restores the working cache on a fresh container.
    Returns the number of files copied. No-op when Blob isn't configured."""
    if not enabled():
        return 0
    client = _container()
    root: Path = cfg.storage_root
    n = 0
    for prefix in prefixes:
        for blob in client.list_blobs(name_starts_with=f"{prefix}/"):
            dst = root / blob.name
            if dst.exists() and _same_content(dst, blob):
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            data = client.download_blob(blob.name).readall()
            dst.write_bytes(data)
            n += 1
    if n:
        log.info("filestore pull: %d file(s) restored from blob", n)
    return n


def push(cfg: Config, prefixes: tuple[str, ...] = PREFIXES) -> int:
    """Local → Blob. Persists new/changed files after a write. Returns the
    number of files uploaded. No-op when Blob isn't configured."""
    if not enabled():
        return 0
    client = _container()
    root: Path = cfg.storage_root
    remote = {b.name: b
              for prefix in prefixes
              for b in client.list_blobs(name_starts_with=f"{prefix}/")}
    n = 0
    for prefix in prefixes:
        base = root / prefix
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            blob = remote.get(rel)
            if blob is not None and _same_content(p, blob):
                continue
            with p.open("rb") as fh:
                client.upload_blob(rel, fh, overwrite=True)
            n += 1
    if n:
        log.info("filestore push: %d file(s) persisted to blob", n)
    return n


def push_async(cfg: Config, prefixes: tuple[str, ...] = PREFIXES) -> None:
    """Fire-and-forget push on a worker thread — for hot paths (a review
    edit) that must not wait on network I/O. Errors are logged, never
    raised: the local file already holds the truth and the next push
    retries the same content."""
    if not enabled():
        return

    def _run() -> None:
        try:
            push(cfg, prefixes)
        except Exception:  # noqa: BLE001
            log.warning("filestore push failed (will retry on next write)",
                        exc_info=True)

    threading.Thread(target=_run, daemon=True).start()
