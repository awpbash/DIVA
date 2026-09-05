"""api/rag/agent.py's run() loop tags + blanks restricted citations on every
tool call, before they reach the citation bundle or the conversation the
model reads on its next turn. Before this fix the value-net check only ran
on the final bundle, so a restricted value the model read mid-loop could
already have leaked into its own streamed "thought" on a later step.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from api.rag import agent
from api.rag import policy as policy_mod
from api.rag.schemas import ChatMessage, Citation, IntentPlan


@pytest.fixture(autouse=True)
def _fresh_policy():
    """Each test reads the YAML seed, not a mutated in-memory policy.
    Matches tests/rag/test_policy_roles.py's fixture."""
    policy_mod._ACTIVE = None
    policy_mod._INDEX = None
    yield
    policy_mod._ACTIVE = None
    policy_mod._INDEX = None


def _restricted_citation() -> Citation:
    # fact_label="Formula" is not itself a denied label in either policy
    # file this repo ships, so restriction here can only come from the value
    # net matching "0.2485" in doc_tokens, the exact loophole tag_citations'
    # doc_tokens argument closes (a mislabelled fact carrying a rate).
    return Citation(
        evidence_id="e1", doc_id="d1", page_no=3,
        snippet="the confidential rate is S$0.2485 per RTh",
        fact_summary="rate = S$0.2485",
        row_context="Rate | S$0.2485 | monthly",
        linked_context="see also S$0.2485",
        verified_by=["reviewer@x.com"],
        fact_label="Formula")


def _clean_citation() -> Citation:
    return Citation(
        evidence_id="e2", doc_id="d1", page_no=4,
        snippet="maintain confidential information", fact_label="Obligation")


def _fake_response(*, tool_calls, content=""):
    message = NS(content=content, tool_calls=tool_calls)
    return NS(choices=[NS(message=message)], usage=None)


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)

    async def create(self, **kwargs):
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.chat = NS(completions=_FakeCompletions(responses))


def _run_agent(monkeypatch, *, role, doc_tokens):
    async def fake_keyword_search(**kwargs):
        # Fresh copies each call, the loop mutates citations in place.
        return [_restricted_citation().model_copy(deep=True),
               _clean_citation().model_copy(deep=True)]

    monkeypatch.setitem(agent.TOOL_DISPATCH, "keyword_search", fake_keyword_search)

    tool_call = NS(id="call_1", function=NS(name="keyword_search", arguments="{}"))
    client = _FakeClient([
        _fake_response(tool_calls=[tool_call], content="looking it up"),
        _fake_response(tool_calls=[], content="done"),
    ])
    plan = IntentPlan(intent="factual")
    messages = [ChatMessage(role="user", content="what is the rate?")]

    async def _collect():
        result = None
        async for item in agent.run(
            client=client, model="gpt-test", store=None, embed_model="embed-test",
            plan=plan, messages=messages, doc_ids=["d1"], catalog="",
            role=role, doc_tokens=doc_tokens,
        ):
            if isinstance(item, agent.AgentResult):
                result = item
        return result

    return asyncio.run(_collect())


def test_a_restricted_citation_is_tagged_and_blanked_before_it_reaches_the_bundle(monkeypatch):
    result = _run_agent(monkeypatch, role="default", doc_tokens={"d1": ["0.2485"]})
    by_id = {c.evidence_id: c for c in result.citations}

    restricted = by_id["e1"]
    assert restricted.restricted is True
    assert restricted.snippet == "[restricted for your access level]"
    assert restricted.fact_summary is None
    assert restricted.row_context is None
    assert restricted.linked_context is None
    assert restricted.verified_by is None

    # An unrelated citation on the same document is left alone.
    clean = by_id["e2"]
    assert clean.restricted is False
    assert clean.snippet == "maintain confidential information"


def test_the_confidential_value_never_reaches_the_transcript(monkeypatch):
    """The timing part of the fix: redaction has to happen before the tool
    result is appended to the conversation, or the model reads the raw value
    on its very next turn regardless of what the final bundle looks like."""
    result = _run_agent(monkeypatch, role="default", doc_tokens={"d1": ["0.2485"]})
    transcript_text = json.dumps(result.transcript)
    assert "0.2485" not in transcript_text


def test_a_cleared_role_is_not_restricted(monkeypatch):
    result = _run_agent(monkeypatch, role="admin", doc_tokens={"d1": ["0.2485"]})
    by_id = {c.evidence_id: c for c in result.citations}
    assert by_id["e1"].restricted is False
    assert by_id["e1"].snippet == "the confidential rate is S$0.2485 per RTh"


def test_doc_tokens_none_skips_the_value_net_entirely(monkeypatch):
    # Documented escape hatch for callers with no role/clearance concept.
    result = _run_agent(monkeypatch, role="default", doc_tokens=None)
    by_id = {c.evidence_id: c for c in result.citations}
    assert by_id["e1"].restricted is False
    assert by_id["e1"].snippet == "the confidential rate is S$0.2485 per RTh"
