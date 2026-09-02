"""Unknown names warn and suggest instead of silently borrowing clauses.

When a question names an entity the resolver cannot bind to any document,
the old behaviour fell back to the whole corpus and the agent answered from
whichever template-twin scored highest. Now the resolver captures the
closest near-miss names on the zero-match path, and the note chat.py injects
tells the model to say "not in the documents" and offer a did-you-mean. A
generic unscoped question (no name attempted) stays warning-free so corpus
recall is untouched.
"""
from __future__ import annotations

import asyncio

from api.rag import resolve
from api.rag.resolve import ScopeResult, unresolved_scope_note


class _FakeStore:
    """Answers the vocabulary queries by matching SQL fragments."""

    def __init__(self, *, docs=(), parties=()):
        self._docs = list(docs)
        self._parties = list(parties)

    async def query(self, sql, params=None, pk=None):
        if "c.kind = 'document'" in sql:
            return self._docs
        if "c.label = 'Party'" in sql:
            return self._parties
        return []


_DOCS = [
    {"doc_id": "docA", "title": "Harbour Point Supply Agreement", "grp": "fam1"},
]
_PARTIES = [
    {"doc_id": "docB", "name": "Northwind Logistics"},
]


def _store():
    return _FakeStore(docs=_DOCS, parties=_PARTIES)


def _scope(question):
    return asyncio.run(resolve.resolve_scope(_store(), question))


# Which words count as distinctive is domain CONFIG, not engine behaviour, so
# pin it. These tests are about the near-miss band, not about any vocabulary.
_STOPWORDS = frozenset({
    "what", "is", "the", "at", "for", "supply", "temperature", "rate",
    "termination", "agreement", "deposits", "chilled", "water",
})


def _patch_registry(monkeypatch):
    monkeypatch.setattr(resolve, "generic_tokens", lambda *a, **k: _STOPWORDS)


def test_zero_match_returns_the_closest_near_miss(monkeypatch):
    # "Harborough" against "Harbour" scores 0.71: under the 0.80 acceptance,
    # over the 0.60 near-miss floor. So it does not scope, and it does suggest.
    _patch_registry(monkeypatch)
    r = _scope("what is the rate at Harborough")
    assert not r.scoped
    assert r.near_misses == ["Harbour Point Supply Agreement"]


def test_near_misses_rank_and_cap(monkeypatch):
    _patch_registry(monkeypatch)
    r = _scope("termination for Northwood")
    assert not r.scoped
    # "Northwood" against "Northwind" scores 0.78, also a near-miss. Nothing in
    # the question comes near the document's own name, so exactly one comes back.
    assert r.near_misses == ["Northwind Logistics"]


def test_generic_question_yields_no_near_misses(monkeypatch):
    _patch_registry(monkeypatch)
    r = _scope("what is the supply temperature")
    assert not r.scoped
    assert r.near_misses == []
    assert unresolved_scope_note(r) == ""


def test_successful_resolution_carries_no_warning(monkeypatch):
    _patch_registry(monkeypatch)
    r = _scope("license fee at Harbour Point")
    assert r.scoped
    assert r.near_misses == []
    assert unresolved_scope_note(r) == ""


def test_note_contains_instruction_and_did_you_mean():
    r = ScopeResult(reason="no entity matched",
                    near_misses=["Harbour Point", "Northwind Logistics"])
    note = unresolved_scope_note(r)
    assert "matched no document" in note
    assert "not in the documents" in note
    assert "Do not borrow clauses" in note
    assert "did you mean" in note
    assert "Harbour Point" in note and "Northwind Logistics" in note


def test_note_is_empty_when_scoped_even_with_leftover_names():
    r = ScopeResult(doc_ids=["docA"], near_misses=["stale"])
    assert unresolved_scope_note(r) == ""
