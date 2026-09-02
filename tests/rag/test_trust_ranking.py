"""Trust is a ranking weight in the aligned-KB lookup: a human-verified
field outranks an equally relevant machine read, disputed sinks below both,
and keyword relevance still leads (an off-topic verified field cannot bury
the on-topic answer)."""
from __future__ import annotations

import asyncio

from api.rag import tools


class _FakeStore:
    """Answers the two queries lookup_verified_fields makes: opsfield
    candidates and document titles."""

    def __init__(self, rows):
        self._rows = rows

    async def query(self, sql, params=None, pk=None):
        if "'opsfield'" in sql:
            return self._rows
        return []   # document title fetch — titles not needed for ranking


def _row(ops_id, trust=None, disputed=False, title="Energy Charge Fees",
         value="0.58 $/RTh", field_key="energy_charge_fees",
         snippet="the energy charge shall be 0.58"):
    return {
        "id": ops_id, "ops_id": ops_id, "kind": "opsfield",
        "doc_id": f"doc-{ops_id}", "field_key": field_key,
        "title": title, "category": "commercial_link", "value": value,
        "snippet": snippet,
        "trust": trust, "verified": trust == "human_validated",
        "disputed": disputed, "is_current": True, "sensitivity": "general",
    }


def _rank(rows, query="energy charge"):
    cites = asyncio.run(tools.lookup_verified_fields(
        store=_FakeStore(rows), query=query, role="admin"))
    return [c.evidence_id for c in cites]


def test_verified_outranks_equal_machine_row():
    rows = [_row("machine"), _row("verified", trust="human_validated")]
    assert _rank(rows)[0] == "ops:verified"


def test_disputed_sinks_below_machine():
    rows = [_row("disputed", disputed=True), _row("machine")]
    assert _rank(rows)[0] == "ops:machine"


def test_relevance_still_leads():
    # A verified but off-topic field must not bury the on-topic machine read.
    rows = [
        _row("machine_on_topic"),
        _row("verified_off_topic", trust="human_validated",
             title="Water Cost Responsibility", value="Customer",
             field_key="water_cost_responsibility",
             snippet="the customer shall bear the charge for water"),
    ]
    assert _rank(rows)[0] == "ops:machine_on_topic"
