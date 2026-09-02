"""Entity scoping: binding a question to the right documents.

The resolver builds a vocabulary of the names that exist in the corpus and
matches the question against it on distinctive tokens, then expands a hit to the
whole document family. Three sources feed that vocabulary: document titles,
party facts, and the identity hubs a document's facts resolve to.

There used to be a fourth, an external register of customers and assets that an
admin maintained alongside the corpus, and most of this file tested it. That
register was one deployment's master data rather than part of the framework and
has been retired, so those tests went with it: they were proving that machinery
worked if it existed, which it no longer does.
"""
from __future__ import annotations

import asyncio

from api.rag import resolve


class _FakeStore:
    """Answers the vocabulary queries by matching SQL fragments."""

    def __init__(self, *, docs=(), parties=(), resolves=(), hubs=()):
        self._docs = list(docs)
        self._parties = list(parties)
        self._resolves = list(resolves)
        self._hubs = list(hubs)

    async def query(self, sql, params=None, pk=None):
        if "c.kind = 'document'" in sql:
            return self._docs
        if "c.label = 'Party'" in sql:
            return self._parties
        if "c.kind = 'hub'" in sql:
            return self._hubs
        if "c.rel = 'RESOLVES_TO'" in sql:
            return self._resolves
        return []


# The fixture titles below are phrased in a domain's vocabulary, and which of
# those words count as distinctive is domain CONFIG, not engine behaviour.
# Pinning the stopword set here keeps these tests about the matching logic,
# which is what they are for. The vocabulary itself is covered by the config
# gates and by test_second_domain.py.
_STOPWORDS = frozenset({
    "license", "supply", "agreement", "amendment", "the", "for",
    "what", "is", "at", "in", "of", "and", "rate", "deposit", "deposits",
    "party", "service", "services",
})


def _patch(monkeypatch) -> None:
    monkeypatch.setattr(resolve, "generic_tokens", lambda *a, **k: _STOPWORDS)


def _scope(store, question):
    return asyncio.run(resolve.resolve_scope(store, question))


_DOCS = [
    {"doc_id": "docA", "title": "Master License Agreement", "grp": "fam1"},
    {"doc_id": "docA2", "title": "First Amendment", "grp": "fam1"},
    {"doc_id": "docB", "title": "Supply Agreement 2020", "grp": "fam2"},
]


def test_extracted_party_name_scopes_to_its_document(monkeypatch):
    _patch(monkeypatch)
    store = _FakeStore(
        docs=_DOCS,
        parties=[{"doc_id": "docB", "name": "Northwind Logistics LLC"}],
    )
    r = _scope(store, "Northwind termination clauses")
    assert r.doc_ids == ["docB"]


def test_a_hit_expands_to_the_whole_family(monkeypatch):
    """A name on one document pulls in its amendments: the chain answers as one
    contract, so scoping to a single link would hide the current value."""
    _patch(monkeypatch)
    store = _FakeStore(
        docs=_DOCS,
        parties=[{"doc_id": "docA", "name": "Vertical Trust (Singapore) Limited"}],
    )
    r = _scope(store, "contract duration for Vertical Trust")
    assert r.scoped and set(r.doc_ids) == {"docA", "docA2"}


def test_identity_hub_name_scopes_whatever_the_hub_is_called(monkeypatch):
    """The hub label is domain configuration. Naming one in the engine is how
    this source came to bind nothing on a domain whose hubs are named
    differently, so the vocabulary reads every hub the store holds."""
    _patch(monkeypatch)
    store = _FakeStore(
        docs=_DOCS,
        resolves=[{"pk": "docA", "tgt": "CanonicalJurisdiction:harbour point"}],
        hubs=[{"id": "CanonicalJurisdiction:harbour point",
               "hub": "CanonicalJurisdiction", "hub_key": "harbour point",
               "hub_name": "Harbour Point"}],
    )
    r = _scope(store, "governing law at Harbour Point")
    assert r.scoped and set(r.doc_ids) == {"docA", "docA2"}


def test_hub_falls_back_to_its_normalised_key_for_a_name(monkeypatch):
    """`model.hub_item` always writes `key`. A display name is optional, so the
    key alone still has to be a usable identity token."""
    _patch(monkeypatch)
    store = _FakeStore(
        docs=_DOCS,
        resolves=[{"pk": "docB", "tgt": "CanonicalParty:lakeside robotics"}],
        hubs=[{"id": "CanonicalParty:lakeside robotics",
               "hub": "CanonicalParty", "hub_key": "lakeside robotics"}],
    )
    r = _scope(store, "what does the Lakeside contract say about penalties")
    assert r.doc_ids == ["docB"]


def test_an_unmatched_name_falls_back_to_the_whole_corpus(monkeypatch):
    """No match must never mean no documents. Narrowing happens only when the
    match is confident, so recall is preserved."""
    _patch(monkeypatch)
    r = _scope(_FakeStore(docs=_DOCS), "tell me about Quantum Harbour")
    assert not r.scoped


def test_id_tokens_never_fuzzy_match_each_other(monkeypatch):
    """BR017 in a question must not scope to BR013. Identifiers one character
    apart are different things, not typo variants of each other."""
    _patch(monkeypatch)
    store = _FakeStore(docs=[
        {"doc_id": "docA", "title": "Supply Agreement BR013", "grp": None},
        {"doc_id": "docB", "title": "Supply Agreement BR017", "grp": None},
    ])
    r = _scope(store, "BR017 supply temperature")
    assert r.doc_ids == ["docB"]
