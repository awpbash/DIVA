"""graph_grep.py: grep the store's text-bearing items for one document (debug helper).

Scans evidence (text_span, embed_text), block (text) and mention (text_span)
items in the document's partition for a substring.

    python -m scripts.graph_grep <doc_id> <substring>
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from pipeline.config import Config
from pipeline.store.client import get_store


def main() -> int:
    load_dotenv()
    doc_id, needle = sys.argv[1], sys.argv[2]
    store = get_store(Config.load())
    rows = store.query(
        "SELECT c.id, c.kind, c.page_no, c.text_span, c.text, c.embed_text FROM c "
        "WHERE ARRAY_CONTAINS(@kinds, c.kind) AND "
        "(CONTAINS(c.text_span, @s) OR CONTAINS(c.text, @s) OR CONTAINS(c.embed_text, @s))",
        [{"name": "@kinds", "value": ["evidence", "block", "mention"]},
         {"name": "@s", "value": needle}],
        pk=doc_id)
    for r in rows:
        text = r.get("text_span") or r.get("text") or r.get("embed_text") or ""
        print(f"{r['id']}  [{r.get('kind')}]  p{r.get('page_no')}  {text[:160]}")
    print(f"-- {len(rows)} hits --")
    return 0


if __name__ == "__main__":
    sys.exit(main())
