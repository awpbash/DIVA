"""Contract tests for the Explore entity-map payload."""
from __future__ import annotations

import asyncio

from api.routes import graph


class _Store:
    """Small async store double shaped around the hub endpoint's read model."""

    def __init__(self, hub_label: str):
        self.hub_id = f"{hub_label}:example-party"
        self.hub_label = hub_label

    async def query(self, sql: str, _params=None):
        if "c.rel = 'HAS_PARTY'" in sql:
            return [{
                "pk": "doc-1", "src": "doc-1", "tgt": self.hub_id,
                "role": "customer", "current": True,
            }]
        if "c.rel = 'RESOLVES_TO'" in sql:
            return [{"pk": "doc-1", "src": "field-1", "tgt": self.hub_id}]
        if "c.kind = 'document'" in sql:
            return [{
                "id": "doc-1", "kind": "document", "doc_id": "doc-1",
                "doctype": "agreement",
            }]
        if "c.kind = 'agreement'" in sql:
            return [{"doc_id": "doc-1", "title": "Example agreement"}]
        if "c.kind = 'hub'" in sql:
            return [{
                "id": self.hub_id, "kind": "hub", "pk": "global",
                "hub": self.hub_label, "key": "example-party", "name": "Example party",
            }]
        if "c.id, c.title, c.field_key" in sql:
            return [{
                "id": "field-1", "title": "Customer", "field_key": "customer",
                "category": "party", "has_evidence": True,
            }]
        raise AssertionError(f"unexpected query: {sql}")


def test_hubs_exposes_direct_party_relation_with_field_support(monkeypatch):
    """The entity map must not turn internal field resolution into its edge."""
    hub_label = next(iter(graph.load_pack().hubs))
    monkeypatch.setattr(graph.deps, "get_store", lambda: _Store(hub_label))

    payload = asyncio.run(graph.hubs())

    assert {n.title for n in payload.nodes} == {"Example agreement", "Example party"}
    assert len(payload.edges) == 1
    edge = payload.edges[0]
    assert edge.type == "HAS_PARTY"
    assert edge.props == {
        "role": "customer",
        "current": True,
        "mentions": 1,
        "fields": ["Customer"],
        "evidence": 1,
    }
