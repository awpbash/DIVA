"""Which domain the bundled sample corpus (examples/corpus/) demonstrates,
and the intake declarations that make it show off the amendment chain (see
examples/README.md for why the corpus is shaped this way).

Read by the setup wizard's Demo path (api/routes/setup.py, api/main.py)
rather than hardcoded there. This lives under examples/ deliberately: the
"no engine module names a domain in code" guard
(tests/extraction/test_active_domain.py) scans `pipeline/`, `api/` and
`scripts/` on purpose and does not scan `examples/`, because example content
is naturally domain-specific — the engine code that CONSUMES it should read
the name from here rather than hardcode it a second time.
"""
from __future__ import annotations

DOMAIN = "commercial_agreement"

CORPUS_DIR = "corpus"

# In upload order. `amends` names another entry's `file` by filename — the
# demo loader resolves it to that document's doc_id after ingesting it,
# since the intake API takes a doc_id, not a filename.
CORPUS: tuple[dict, ...] = (
    {"file": "01-master-license-agreement-2023.pdf",
     "document_type": "Agreement", "document_date": "2023-03-14"},
    {"file": "02-first-amendment-2024.pdf",
     "document_type": "Amendment", "document_date": "2024-09-02",
     "relation": "amends", "amends": "01-master-license-agreement-2023.pdf"},
    {"file": "03-second-amendment-2025.pdf",
     "document_type": "Amendment", "document_date": "2025-01-20",
     "relation": "amends", "amends": "01-master-license-agreement-2023.pdf"},
)
