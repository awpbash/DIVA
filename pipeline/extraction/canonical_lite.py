"""canonical_lite.py — the schema-first stand-in for understand/validate/repair.

Writes a minimal, fact-less ``canonical/<doc_id>.json`` so the graph loader can
build the document's structure tier (Document / Section / Block) without the
legacy open-vocab fact extraction. The ops-view extraction
(``pipeline.kb.field_llm``) plus the review/KM layer carry the document's
knowledge instead; raw text stays searchable via blocks/sections.

REFUSES to overwrite an existing canonical: re-running a legacy document under
schema-first mode must never strip its extracted facts.
"""
from __future__ import annotations

import json

from ..config import Config
from ..ontology import DEFAULT_DOCTYPE
from ..storage import Paths


def run(cfg: Config, doc_id: str, *, doctype: str = DEFAULT_DOCTYPE) -> dict:
    paths = Paths(cfg)
    out = paths.canonical_json(doc_id)
    if out.exists():
        existing = json.loads(out.read_text(encoding="utf-8"))
        n = len(existing.get("facts") or [])
        print(f"[canonical_lite] canonical exists ({n} facts) — left untouched")
        return existing
    payload = {
        "doc_id": doc_id,
        "doctype": doctype,
        "analyzer_id": "_universal",
        "extraction_mode": "schema_first",
        "facts": [],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[canonical_lite] wrote fact-less canonical -> {out.name}")
    return payload
