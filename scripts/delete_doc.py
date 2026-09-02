"""scripts/delete_doc.py — remove ONE document from everywhere: the Cosmos
container (its partition + any global items that reference it), every
storage/ artifact, and its registry/jobs/votes rows. Then a KM rebuild so
derived global state (catalog, parties, timeline) forgets it too.

    python -m scripts.delete_doc <doc_id>            # local emulator store
    python -m scripts.delete_doc <doc_id> --live     # the real Azure account
    ... --keep-km        skip the KM rebuild (batch deletes: rebuild once at the end)

Destructive by design; the doc can always be re-ingested from its PDF.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path


def _map_live_env() -> None:
    env = {l.split("=", 1)[0]: l.split("=", 1)[1].strip()
           for l in Path(".env").read_text(encoding="utf-8").splitlines()
           if "=" in l and not l.startswith("#")}
    uri = env.get("COSMOS_LIVE_URI", "")
    if not (uri.startswith("https://") and ".documents.azure.com" in uri):
        raise SystemExit("COSMOS_LIVE_URI missing/invalid in .env")
    os.environ["COSMOS_URI"] = uri
    os.environ["COSMOS_KEY"] = env["COSMOS_LIVE_KEY"]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        raise SystemExit("usage: python -m scripts.delete_doc <doc_id> [--live] [--keep-km]")
    doc_id = args[0]
    live = "--live" in sys.argv
    if live:
        _map_live_env()

    from pipeline.config import Config
    from pipeline.store import get_store

    cfg = Config.load()
    store = get_store(cfg)

    # 1) Cosmos: the doc's partition, then global items that point at it.
    n_pk = store.delete_where("c.pk = @pk", [{"name": "@pk", "value": doc_id}])
    n_glob = store.delete_where(
        "c.pk = 'global' AND c.doc_id = @doc", [{"name": "@doc", "value": doc_id}])
    print(f"[cosmos] deleted {n_pk} partition items + {n_glob} global items "
          f"({'LIVE' if live else 'local emulator'})")

    # 2) storage/ artifacts: <id>.* files and <id>/ dirs in every subdir.
    root: Path = cfg.storage_root
    removed = []
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        d = sub / doc_id
        if d.is_dir():
            shutil.rmtree(d)
            removed.append(f"{sub.name}/{doc_id}/")
        for f in sub.glob(f"{doc_id}*"):
            if f.is_file():
                f.unlink()
                removed.append(f"{sub.name}/{f.name}")
    print(f"[files ] removed {len(removed)}: {', '.join(removed) or 'none'}")

    # 2b) Children that declared THIS doc as their parent: a dangling
    # parent_doc_id 400s every later intake edit (unknown parent), so reset
    # them to standalone and say so loudly.
    from pipeline.storage import atomic_write_json
    orphans = []
    raw_dir = root / "raw"
    if raw_dir.is_dir():
        for mp in sorted(raw_dir.glob("*.meta.json")):
            try:
                sidecar = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            declared = sidecar.get("intake") or {}
            if declared.get("parent_doc_id") != doc_id:
                continue
            declared["relation"] = "standalone"
            declared.pop("parent_doc_id", None)
            sidecar["intake"] = declared
            atomic_write_json(mp, sidecar, indent=None)
            orphans.append(mp.name[:-len(".meta.json")])
    if orphans:
        print(f"[WARN  ] {len(orphans)} document(s) declared {doc_id} as their "
              f"parent: {', '.join(orphans)}")
        print("[WARN  ] their relation was reset to standalone (parent link "
              "dropped) so intake edits keep working. Re-declare their "
              "relations if they belong to another chain.")

    # 3) app.db rows.
    con = sqlite3.connect(root / "app.db")
    for table in ("registry_documents", "jobs", "field_votes"):
        try:
            n = con.execute(f"DELETE FROM {table} WHERE doc_id=?", (doc_id,)).rowcount
            if n:
                print(f"[app.db] {table}: {n} row(s)")
        except sqlite3.OperationalError:
            pass
    con.commit()
    con.close()

    # 4) KM rebuild so catalog/parties/timeline forget the doc.
    if "--keep-km" not in sys.argv:
        from pipeline.kb import km as km_mod
        print("[km    ]", km_mod.build_km(cfg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
