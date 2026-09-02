"""lookup_section RBAC: confidential spans are filtered at the source.

The leak under test: lookup_section returned every evidence span of a matched
section regardless of viewer role, while the vector tools already filtered
spans citing confidential blocks. The same `_span_visible` gate now applies,
and cleared roles keep identical behavior.
"""
from __future__ import annotations

import asyncio

from api.rag.tools import lookup_section


class _FakeStore:
    def __init__(self, sections, evidence):
        self._sections = list(sections)
        self._evidence = list(evidence)

    async def query(self, sql, params=None, pk=None):
        p = {x["name"]: x["value"] for x in (params or [])}
        if "c.section_num = @num" in sql:
            return [s for s in self._sections
                    if s.get("section_num") == p["@num"]]
        if "c.kind = 'evidence' AND c.section_id = @sid" in sql:
            return [e for e in self._evidence
                    if e.get("section_id") == p["@sid"]]
        if "ARRAY_CONTAINS(@ids, c.id)" in sql:
            ids, kind = set(p["@ids"]), p.get("@kind")
            return [s for s in self._sections
                    if s["id"] in ids and (kind is None or s.get("kind") == kind)]
        return []


_SECTION = {"id": "s1", "kind": "section", "doc_id": "d1", "section_num": "9.3",
            "title": "Charges", "page_start": 4}

_EVIDENCE = [
    {"id": "ev_open", "kind": "evidence", "section_id": "s1", "doc_id": "d1",
     "page_no": 4, "text_span": "general clause text"},
    {"id": "ev_conf", "kind": "evidence", "section_id": "s1", "doc_id": "d1",
     "page_no": 5, "text_span": "S$ 123,456 secret charge",
     "cites_confidential": True},
]


def _run(role):
    store = _FakeStore([_SECTION], _EVIDENCE)
    return asyncio.run(lookup_section(store=store, section_num="9.3", role=role))


def test_uncleared_role_never_sees_confidential_spans():
    ids = {c.evidence_id for c in _run(None)}
    assert ids == {"ev_open"}


def test_default_role_is_uncleared_too():
    ids = {c.evidence_id for c in _run("default")}
    assert ids == {"ev_open"}


def test_cleared_roles_see_everything():
    for role in ("admin", "finance", "confidential"):
        ids = {c.evidence_id for c in _run(role)}
        assert ids == {"ev_open", "ev_conf"}, role


def test_all_confidential_section_degrades_to_heading_anchor():
    # Every span filtered: the section still answers with its heading
    # (layout info only), never with the hidden text.
    store = _FakeStore(
        [_SECTION],
        [e for e in _EVIDENCE if e.get("cites_confidential")])
    cits = asyncio.run(lookup_section(store=store, section_num="9.3", role=None))
    assert [c.evidence_id for c in cits] == ["s1:heading"]
    assert cits[0].snippet == "Charges"
