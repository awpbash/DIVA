"""scripts/reset_dev.py — wipe an instance back to a genuinely fresh checkout.

    python -m scripts.reset_dev                # asks for confirmation first
    python -m scripts.reset_dev --yes           # skip the prompt (CI / scripting)
    python -m scripts.reset_dev --yes --wipe-key  # also forget OPENAI_API_KEY

Also reachable from inside a running instance: an admin can trigger the exact
same ``perform_reset()`` from Admin > Accounts > "Reset this instance", when
the operator has set ``VERBATIM_ALLOW_RESET=1`` (see
``POST /admin/dev-reset`` in api/routes/admin.py). One function, two front
doors, so "what does a reset actually wipe" has a single definition rather
than a CLI version and a web version quietly drifting apart.

What this wipes:
  - every app-database TABLE (accounts, sessions, ontology edits, jobs,
    feedback, activity, usage, chat history, settings, ...). Rows are
    DELETEd, the file itself is left in place — safe to call from a live
    process serving requests through that same file, unlike removing it out
    from under an open connection (a real Windows failure mode; see
    scripts/delete_doc.py for the same file-not-process assumption already
    used elsewhere in this codebase). Table names are read from
    ``sqlite_master`` rather than hand-listed, so this stays correct as the
    schema grows new tables later.
  - every OTHER file under storage/ (uploaded PDFs, OCR/extraction caches,
    review records). These have no open handles, so plain deletion is fine.
    app.db's WAL sidecar files (app.db-wal, app.db-shm) are kept alongside
    app.db itself for the same reason: a live connection keeps them mapped.
  - the Cosmos knowledge-base container (``CosmosStore.nuke()`` — drop +
    recreate, the same "universal rollback lever" scripts/rebuild_kb.py
    already uses to wipe the KB for a rebuild).
  - ``configs/pipeline.yaml``'s ``domain:`` key, where one was declared.
  - the ``setup_pending`` flag in the (just-wiped, then rewritten) app db.
    This is the one that actually matters for "does the wizard come back".
    Wiping the app database regrows a fresh default admin for free on the
    very next boot, and a checkout that ships only one domain pack
    autodetects it regardless of the ``domain:`` key above — so with the
    key left in place (the default), ``api/appdb.py:is_setup_complete``'s
    inferred fallback (domain resolves + an admin exists + a real key) would
    read True again on that next boot, before the wizard ever shows up.
    This flag overrides that fallback until the wizard's own ``/setup/finish``
    runs again.

What this leaves alone by default:
  - ``.env``'s ``OPENAI_API_KEY`` — forcing a real key to be re-typed and
    re-verified on every reset cycle would make this too annoying to use
    for its actual purpose (repeatedly testing first-run onboarding). Pass
    ``--wipe-key`` for a truly total reset that forgets it too.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PIPELINE_YAML = ROOT / "configs" / "pipeline.yaml"
_ENV_PATH = ROOT / ".env"


def _wipe_appdb_rows(db_path: Path) -> list[str]:
    """DELETE every row of every table. See the module docstring for why
    this truncates in place rather than deleting the file."""
    if not db_path.exists():
        return []
    wiped: list[str] = []
    with sqlite3.connect(db_path) as c:
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            n = c.execute(f'DELETE FROM "{t}"').rowcount
            if n:
                wiped.append(f"{t} ({n} rows)")
        c.commit()
    return wiped


def _wipe_storage_files(storage_root: Path, keep: set[str]) -> list[str]:
    """Delete everything directly under storage/ except the names in
    ``keep`` (the app database, truncated separately, plus its WAL sidecar
    files). Those sidecars ARE open-handle risk from a live process, same as
    app.db itself: WAL mode keeps app.db-wal/-shm memory-mapped for as long
    as any connection is open, and Windows refuses to unlink a mapped file
    (the same failure mode the app.db skip already exists for). Every other
    file here has no open handles, so plain deletion is fine."""
    if not storage_root.exists():
        return []
    removed: list[str] = []
    for p in sorted(storage_root.iterdir()):
        if p.name in keep:
            continue
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
        removed.append(p.name)
    return removed


def _mark_setup_pending(db_path: Path) -> None:
    """Write the same flag api/appdb.py:mark_setup_pending writes, in raw
    SQL rather than importing the api package, since a checkout might reset
    an app.db from a process that isn't running the app at all."""
    with sqlite3.connect(db_path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings "
                  "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                  ("setup_pending", "1"))
        c.commit()


def _clear_domain_declaration() -> bool:
    """Remove configs/pipeline.yaml's `domain:` key — the twin of
    api/routes/setup.py's `_write_domain`, kept as its own tiny copy here
    rather than a shared import so this script has no dependency on the API
    package. Returns whether there was anything to clear."""
    if not _PIPELINE_YAML.exists():
        return False
    import yaml
    doc = yaml.safe_load(_PIPELINE_YAML.read_text(encoding="utf-8")) or {}
    if "domain" not in doc:
        return False
    del doc["domain"]
    _PIPELINE_YAML.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return True


def _wipe_key() -> bool:
    if not _ENV_PATH.exists():
        return False
    from dotenv import unset_key
    ok, *_rest = unset_key(str(_ENV_PATH), "OPENAI_API_KEY")
    return bool(ok)


def perform_reset(*, wipe_key: bool = False) -> dict:
    """The one function both the CLI below and the admin dashboard's "reset
    this instance" button call. Best-effort on the Cosmos step: a store
    that's unreachable (not running, wrong credentials) must not stop the
    rest of the reset — an operator resetting a broken instance is exactly
    who needs the app-side wipe to still go through."""
    from pipeline.config import Config
    cfg = Config.load()

    db_path = cfg.storage_root / "app.db"
    report: dict = {
        "app_db_tables": _wipe_appdb_rows(db_path),
        "storage_files": _wipe_storage_files(
            cfg.storage_root, keep={"app.db", "app.db-wal", "app.db-shm"}),
    }
    _mark_setup_pending(db_path)
    try:
        from pipeline.store.client import get_store
        report["kb_items_dropped"] = get_store(cfg).nuke()
    except Exception as exc:  # noqa: BLE001 — best-effort, see docstring
        report["kb_error"] = f"{type(exc).__name__}: {exc}"
    report["domain_cleared"] = _clear_domain_declaration()
    report["key_cleared"] = _wipe_key() if wipe_key else False
    return report


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.reset_dev",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--wipe-key", action="store_true",
                    help="also clear OPENAI_API_KEY from .env (left alone by default)")
    args = ap.parse_args()

    print("\nThis wipes every account, uploaded document, and extracted fact on "
          "this instance, drops the knowledge-base container, and clears the "
          "active domain so the setup wizard runs again on the next boot.")
    if args.wipe_key:
        print("It will ALSO clear OPENAI_API_KEY from .env.")
    print("This cannot be undone.\n")
    if not args.yes:
        reply = input("Type 'reset' to continue: ").strip().lower()
        if reply != "reset":
            print("Cancelled — nothing was changed.")
            return 1

    report = perform_reset(wipe_key=args.wipe_key)
    print("\n[app.db ] " + (", ".join(report["app_db_tables"]) or "already empty"))
    print("[storage] removed: " + (", ".join(report["storage_files"]) or "nothing else to remove"))
    if "kb_error" in report:
        print(f"[kb     ] could not reach the knowledge store — {report['kb_error']}\n"
              f"          (safe to ignore if it simply isn't running right now)")
    else:
        print(f"[kb     ] dropped and recreated the container "
              f"({report['kb_items_dropped']} item(s) were in it)")
    print(f"[domain ] {'cleared' if report['domain_cleared'] else 'was already unset'}")
    if args.wipe_key:
        print(f"[key    ] {'cleared' if report['key_cleared'] else 'was already unset'}")

    print("\nDone. This instance is now a fresh install — start it and the setup "
          "wizard will run again:\n\n    docker compose up -d\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
