"""load_real_world_corpus.py: ingest examples/real_world/corpus/ through the
real upload path, same mechanism api/main.py's _load_demo_corpus uses for the
flagship 3-document demo, pointed at examples/real_world/manifest.py instead.

Opt-in, not run at boot. Run once against a live instance:

    python -m scripts.load_real_world_corpus

On Docker Desktop for Windows, run it with the app container stopped
(`docker compose stop app && docker compose run --rm app python -m
scripts.load_real_world_corpus`, then start the app again). A second
process opening storage/app.db over that bind mount while the app is
running can fail with "unable to open database file", see
docs/troubleshooting.md's Windows section.

Idempotent: re-running after the first successful pass is a no-op, since
ingest_local_pdf skips documents already in the knowledge base.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path


async def main() -> None:
    from api import appdb, deps
    from api.routes.admin import ingest_local_pdf
    from api.settings import get_config
    from examples.real_world.manifest import CORPUS, CORPUS_DIR
    from pipeline.storage import fields_dir

    appdb.init()
    await deps.init()
    cfg = get_config()

    corpus_root = Path(__file__).resolve().parents[1] / "examples" / "real_world" / CORPUS_DIR
    paths = {item["file"]: corpus_root / item["file"] for item in CORPUS}
    missing = [name for name, p in paths.items() if not p.exists()]
    if missing:
        print(f"missing under {corpus_root}: {missing}", file=sys.stderr)
        sys.exit(1)

    admins = [u for u in appdb.list_users() if u["role"] == "admin"]
    admin_email = admins[0]["email"] if admins else appdb.default_bootstrap_email()

    doc_ids: dict[str, str] = {}
    for item in CORPUS:
        parent = item.get("amends")
        doc_id = ingest_local_pdf(
            paths[item["file"]], admin_email=admin_email,
            document_type=item["document_type"], document_date=item["document_date"],
            relation=item.get("relation"),
            parent_doc_id=doc_ids.get(parent) if parent else None,
        )
        doc_ids[item["file"]] = doc_id
        print(f"queued {item['file']} -> {doc_id}")

    print(f"\n{len(doc_ids)} documents queued for extraction, waiting for it to finish...")
    pending = set(doc_ids.values())
    deadline = time.monotonic() + 20 * 60
    while pending and time.monotonic() < deadline:
        done = {d for d in pending if (fields_dir(cfg) / f"{d}.json").exists()}
        pending -= done
        for d in done:
            print(f"  done: {d}")
        if pending:
            time.sleep(5)
    if pending:
        print(f"still extracting after 20 minutes, not finished: {sorted(pending)}",
              file=sys.stderr)
        sys.exit(1)
    print("all documents extracted.")


if __name__ == "__main__":
    asyncio.run(main())
