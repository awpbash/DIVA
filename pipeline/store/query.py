"""store/query.py — pure query helpers (no Cosmos client, unit-testable).

Two jobs:

  1. SQL fragments for the Cosmos NoSQL dialect (parameterised IN via
     ARRAY_CONTAINS, safe LIKE/CONTAINS token filters).
  2. Exact in-RAM vector ranking — the dev/default vector mode. The corpus
     is a few thousand 3072-dim embeddings (~90 MB float32); exact cosine in
     numpy is fast, deterministic and emulator-proof. Native Cosmos
     VectorDistance (DiskANN) is the opt-in for a grown corpus on real Azure.
"""
from __future__ import annotations

import math
import re
from typing import Iterable

import numpy as np


# ---------------------------------------------------------------------------
# SQL builder bits
# ---------------------------------------------------------------------------


def inject_pk(sql: str, params: list[dict],
              pk: str | None) -> tuple[str, list[dict]]:
    """Force the partition filter INTO the SQL when a pk is given.

    The SDK's ``partition_key=`` routing should be enough, but the local
    vnext emulator has been observed returning ALL partitions regardless —
    a silent cross-doc leak. Belt and braces: the predicate goes into the
    WHERE clause too, which is correct everywhere and free on real Azure
    (the partition filter is exactly what the router wants to see).

    Every store query is written as ``SELECT ... FROM c [WHERE <and-chain>]``
    with any ORs parenthesised, so splicing ``c.pk = @__pk AND`` directly
    after WHERE preserves semantics."""
    if pk is None:
        return sql, params
    params = params + [{"name": "@__pk", "value": pk}]
    m = re.search(r"\bWHERE\b", sql, flags=re.IGNORECASE)
    if not m:
        return sql + " WHERE c.pk = @__pk", params
    # Parenthesise the condition chain only — stop before a trailing
    # ORDER BY / GROUP BY / OFFSET clause if one exists.
    tail = re.search(r"\b(ORDER\s+BY|GROUP\s+BY|OFFSET)\b", sql[m.end():],
                     flags=re.IGNORECASE)
    cond_end = m.end() + (tail.start() if tail else len(sql) - m.end())
    return (sql[:m.end()] + " c.pk = @__pk AND (" + sql[m.end():cond_end]
            + ") " + sql[cond_end:]), params


def in_clause(field: str, values: list, params: list[dict], name: str) -> str:
    """``ARRAY_CONTAINS(@name, c.field)`` with the list bound as ONE param —
    the Cosmos-friendly IN. Appends to ``params`` in place."""
    params.append({"name": f"@{name}", "value": list(values)})
    return f"ARRAY_CONTAINS(@{name}, c.{field})"


def scope_clause(doc_ids: list[str] | None, params: list[dict],
                 field: str = "doc_id") -> str:
    """Document-scope filter; empty scope = whole corpus (TRUE)."""
    if not doc_ids:
        return "true"
    return in_clause(field, doc_ids, params, "scope_ids")


# ---------------------------------------------------------------------------
# Exact vector ranking (client mode)
# ---------------------------------------------------------------------------


class RamVectors:
    """An in-memory exact vector index over one kind's embeddings.

    Rows: (id, doc_id, unit-normalised float32 vector). Cosine similarity is
    then one matmul. Built once per process from a Cosmos scan; the caller
    checks the build stamp to know when to reload.
    """

    def __init__(self, rows: Iterable[tuple[str, str, list[float]]]) -> None:
        ids: list[str] = []
        docs: list[str] = []
        vecs: list[np.ndarray] = []
        for item_id, doc_id, emb in rows:
            v = np.asarray(emb, dtype=np.float32)
            n = float(np.linalg.norm(v))
            if not n or math.isnan(n):
                continue
            ids.append(item_id)
            docs.append(doc_id or "")
            vecs.append(v / n)
        self.ids = ids
        self.doc_ids = docs
        self.matrix = (np.vstack(vecs) if vecs
                       else np.zeros((0, 1), dtype=np.float32))

    def __len__(self) -> int:
        return len(self.ids)

    def top_k(self, query_vec: list[float], k: int,
              doc_ids: set[str] | None = None) -> list[tuple[str, float]]:
        """[(id, cosine_similarity)] best-first, optionally doc-scoped."""
        if not self.ids:
            return []
        q = np.asarray(query_vec, dtype=np.float32)
        n = float(np.linalg.norm(q))
        if not n:
            return []
        sims = self.matrix @ (q / n)
        if doc_ids:
            mask = np.fromiter((d in doc_ids for d in self.doc_ids),
                               dtype=bool, count=len(self.doc_ids))
            sims = np.where(mask, sims, -1.0)
        k = min(k, len(self.ids))
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        return [(self.ids[i], float(sims[i])) for i in idx if sims[i] > -1.0]


# ---------------------------------------------------------------------------
# Keyword scoring (the BM25 stand-in for Lucene fulltext)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.\-/]{1,}")

_STOP = {
    "the", "a", "an", "of", "to", "in", "for", "and", "or", "is", "are",
    "be", "on", "at", "by", "with", "as", "it", "this", "that", "shall",
    "any", "all", "not", "no", "from", "under", "per",
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP]


def keyword_score(query_tokens: list[str], text: str) -> float:
    """TF-saturated keyword score (BM25-shaped, no corpus statistics): each
    query token contributes tf/(tf+1), longer texts damped. Deterministic and
    good enough at this corpus size to replace the Lucene index rank."""
    if not query_tokens:
        return 0.0
    toks = tokenize(text)
    if not toks:
        return 0.0
    tf: dict[str, int] = {}
    for t in toks:
        tf[t] = tf.get(t, 0) + 1
    score = 0.0
    for qt in query_tokens:
        f = tf.get(qt, 0)
        if f:
            score += f / (f + 1.0)
    return score / (1.0 + math.log1p(len(toks) / 100.0))
