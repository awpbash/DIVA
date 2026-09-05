"""Tool-calling agent loop.

Wraps OpenAI's chat-completion tool-calling. The loop:

1. System prompt seeded with the planner's ``IntentPlan`` (intent + key_terms +
   categories + rationale) — gives the agent a head-start on which tool to
   reach for first.
2. Each iteration: model emits 0+ tool calls, we dispatch them in parallel,
   union the citations into a bundle keyed by ``evidence_id``, append the
   tool messages to the conversation, repeat.
3. Stop when the model emits no tool calls (or calls ``finish``), or when
   ``max_steps`` is hit.
4. Yield ``AgentEvent`` objects throughout so the SSE route can stream
   per-step thoughts, tool calls, and incremental citations to the UI.

The loop returns / accumulates the citation bundle; synth runs separately
on the final union. Keeping retrieval and answer-generation separate means
the agent can iterate on retrieval cheaply, then we pay for streaming
synth once with the full evidence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from pipeline.store.aio import STORE_UNAVAILABLE_ERRORS, AsyncCosmosStore
from openai import AsyncOpenAI

from ..settings import get_settings
from . import policy as policy_mod
from .prompts import agent_system
from .schemas import ChatMessage, Citation, IntentPlan
from .tools import (
    OPENAI_TOOL_SCHEMAS,
    TOOL_DISPATCH,
    serialize_tool_result,
)

log = logging.getLogger("rag.agent")


# ---------------------------------------------------------------------------
# Event payloads (route layer wraps these into SSE)
# ---------------------------------------------------------------------------


@dataclass
class AgentEvent:
    """One event emitted by the loop. ``kind`` is the SSE event name."""
    kind: str               # "step" | "tool_call" | "tool_result" | "agent_done"
    data: dict[str, Any]    # JSON-serialisable payload


@dataclass
class AgentResult:
    """Final accumulated state. Returned after the loop finishes."""
    citations: list[Citation]
    steps_used: int
    finish_reason: str
    transcript: list[dict] = field(default_factory=list)  # OpenAI messages
    # Aggregations: one entry per aggregating tool call. Synth needs these
    # so a "how many X" question can be answered with the TRUE count rather
    # than the sample size (sample is capped at 5).
    aggregations: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt seeding
# ---------------------------------------------------------------------------


def _format_plan_brief(plan: IntentPlan) -> str:
    """Render the planner output as a brief the agent reads up front."""
    bits = [
        f"intent: {plan.intent}",
        f"key_terms: {', '.join(plan.key_terms) if plan.key_terms else '(none)'}",
        f"categories: {', '.join(plan.categories) if plan.categories else '(none)'}",
        f"needs_table: {plan.needs_table}",
    ]
    if plan.rationale:
        bits.append(f"why: {plan.rationale}")
    return "\n".join(bits)


def _format_history(messages: list[ChatMessage]) -> str:
    if not messages:
        return "(this is the first turn)"
    return "\n".join(f"{m.role}: {m.content}" for m in messages)


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


def _enabled_tool_schemas() -> list[dict]:
    """Tool schemas minus anything in CHAT_DISABLED_TOOLS — the kill-switch
    for a misbehaving tool (env var + restart, no code change)."""
    disabled = set(get_settings().disabled_tools)
    if not disabled:
        return OPENAI_TOOL_SCHEMAS
    return [s for s in OPENAI_TOOL_SCHEMAS
            if s["function"]["name"] not in disabled]


async def _dispatch_tool(
    *, name: str, args: dict, store: AsyncCosmosStore, client: AsyncOpenAI,
    embed_model: str, doc_ids: list[str] | None,
    embed_client: AsyncOpenAI | None = None, role: str | None = None,
    ops_scope: dict | None = None,
) -> Any:
    """Run one tool call. Adds the per-tool dependencies the schemas don't
    expose (store, client, embed_model, doc_ids) so the agent never has
    to pass infrastructure args."""
    if name == "finish":
        return {"ok": True}
    if name in get_settings().disabled_tools:
        return []
    fn = TOOL_DISPATCH.get(name)
    if fn is None:
        return []
    kwargs: dict[str, Any] = dict(args or {})

    # Per-tool dependency injection. Stays here (not in tools.py) so the
    # tools themselves keep clean signatures.
    if name in ("vector_search_evidence", "vector_search_sections",
                "vector_search_blocks"):
        # Vector tools use the client ONLY to embed the query → route them to
        # the embeddings client (may target a different endpoint than chat).
        # All three carry raw text the answer policy can't always key on, so
        # they ALSO filter confidential blocks/spans at the source, per role.
        kwargs.update(store=store, client=(embed_client or client),
                      embed_model=embed_model, doc_ids=doc_ids, role=role)
    elif name in ("lookup_verified_fields", "aggregate_ops_fields",
                  "keyword_search", "lookup_defined_term", "lookup_section",
                  "find_orphan_fragments", "find_document_assets"):
        # These filter confidential content AT THE SOURCE (ops-field
        # sensitivity / raw-block sensitivity / span cites_confidential),
        # so they need the viewer's role (unlike the typed-fact tools,
        # which are filtered post-hoc by the policy layer).
        kwargs.update(store=store, doc_ids=doc_ids, role=role)
        if name in ("lookup_verified_fields", "aggregate_ops_fields"):
            # OPS layer only: a user-pinned amendment inherits current rows
            # from unpinned family siblings, marked carried-over. Raw-text
            # and fact tools above stay strictly pinned via doc_ids.
            kwargs.update(ops_scope=ops_scope)
    elif name == "expand_cross_refs":
        # Evidence-span results, so the same at-source span filter applies.
        kwargs.update(store=store, role=role)

    t0 = time.monotonic()
    print(f"[tool] {name} → {args}", flush=True)
    try:
        result = await fn(**kwargs)
        print(f"[tool] {name} ← {time.monotonic()-t0:.2f}s", flush=True)
        return result
    except STORE_UNAVAILABLE_ERRORS:
        # Store down/waking: NO tool can succeed, so abort the whole turn
        # instead of feeding the model error strings it would spend tokens
        # narrating around. chat.py turns this into a friendly "still waking
        # up, retry shortly" stream error.
        print(f"[tool] {name} ✗ store unavailable", flush=True)
        raise
    except Exception as exc:  # noqa: BLE001 — surface tool errors back to the model
        print(f"[tool] {name} ✗ {type(exc).__name__}: {exc}", flush=True)
        log.exception("tool %s failed", name)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _merge_citations(
    bundle: dict[str, Citation], new: list[Citation],
) -> list[Citation]:
    """Union by evidence_id, keep the higher score on conflict. Returns the
    list of citations that are new in this step (for event payloads)."""
    added: list[Citation] = []
    for c in new:
        if not c.evidence_id:
            continue
        existing = bundle.get(c.evidence_id)
        if existing is None or c.score > existing.score:
            bundle[c.evidence_id] = c
            if existing is None:
                added.append(c)
    return added


# Tools whose results are exact lookups or true aggregations rather than
# fuzzy recall. Their citations stay on top of the bundle so RRF can't bury an
# exact value or a true count under a chatty span.
_AUTHORITATIVE_TOOLS = frozenset({
    "lookup_section", "lookup_defined_term", "lookup_verified_fields",
    "aggregate_ops_fields",
})


def _rrf_scores(ranked_lists: list[list[str]], k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion over per-tool-call ranked id-lists.

    Each retriever (vector, BM25, …) returns its own ranking; RRF fuses them
    by RANK, not score — which is exactly why it sidesteps the incomparable-
    score problem (cosine vs BM25 vs fixed typed scores). A span ranked high
    by several retrievers accumulates several 1/(k+rank) terms and rises; a
    span seen by only one still scores, but lower. ``k`` damps the tail
    (standard default 60). rank is 1-based so the top hit contributes 1/(k+1).
    """
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, eid in enumerate(lst, start=1):
            if eid:
                scores[eid] = scores.get(eid, 0.0) + 1.0 / (k + rank)
    return scores


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def run(
    *, client: AsyncOpenAI, model: str, store: AsyncCosmosStore, embed_model: str,
    plan: IntentPlan, messages: list[ChatMessage],
    doc_ids: list[str] | None, max_steps: int = 5,
    catalog: str = "", meter=None, doc_titles: dict[str, str] | None = None,
    embed_client: AsyncOpenAI | None = None, role: str | None = None,
    ops_scope: dict | None = None, scope_note: str = "",
    doc_tokens: dict[str, list[str]] | None = None,
) -> AsyncIterator[AgentEvent | AgentResult]:
    """Run the tool-calling loop. Yields ``AgentEvent`` instances during the
    loop and one final ``AgentResult`` when it terminates.

    The route layer matches on type: events get re-emitted as SSE, the
    result feeds synth.

    ``doc_tokens`` is the confidential-value net (api/rag/policy.py), applied
    to each tool call's results BEFORE they are appended to the conversation
    or yielded as a tool_result event. The per-tool source filters already
    keep most confidential facts out of a result entirely. This is the
    belt-and-braces catch for the ones that slip past that first check
    (a mislabelled fact, or a visible citation's row_context smuggling a
    restricted value past the class check, same as api/rag/policy.py's
    tag_citations already documents). Applying it here, not just on the
    final bundle after the loop, matters because a restricted value the
    model READS can otherwise resurface in its own free-text "thought" on a
    later step, which streams to the browser as it's generated, well before
    any post-loop filter would ever run. None skips the check entirely
    (callers with no role/clearance concept, if any ever exist)."""
    if not messages:
        yield AgentResult(citations=[], steps_used=0, finish_reason="empty_input")
        return

    user_question = messages[-1].content
    history = _format_history(messages[:-1])
    plan_brief = _format_plan_brief(plan)

    # Plain replace (not str.format) — the prompt body has literal `{...}`
    # examples like `{rate_type:"consumption_charge_rate"}` that would
    # otherwise be interpreted as format placeholders.
    system_prompt = (
        agent_system()
        .replace("{catalog}", catalog or "(catalog unavailable)")
        .replace("{plan_brief}", plan_brief)
        .replace("{history}", history)
        .replace("{question}", user_question)
    )
    if scope_note:
        # Unknown-name warning from the resolver (chat.py): the question
        # named an entity no document matches. Rides at the end so it is the
        # freshest instruction the model reads.
        system_prompt += "\n\n" + scope_note

    convo: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_question},
    ]

    bundle: dict[str, Citation] = {}
    aggregations: list[dict[str, Any]] = []
    # RRF inputs: one ranked id-list per tool call that returned citations,
    # plus the ids that came from authoritative graph tools (kept on top).
    retrieval_lists: list[list[str]] = []
    privileged_ids: set[str] = set()
    finish_reason = "max_steps"
    step = 0

    while step < max_steps:
        step += 1
        t0 = time.monotonic()
        print(f"[agent] step={step} → openai (model={model}, "
              f"convo_msgs={len(convo)})", flush=True)
        log.info("agent step=%d → openai (model=%s, convo_msgs=%d)",
                 step, model, len(convo))
        resp = await client.chat.completions.create(
            model=model,
            messages=convo,
            tools=_enabled_tool_schemas(),
            tool_choice="auto",
            max_completion_tokens=1200,
            parallel_tool_calls=True,
        )
        if meter is not None:
            meter.add("agent", resp.usage)
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []
        print(f"[agent] step={step} ← openai {time.monotonic()-t0:.2f}s "
              f"tool_calls={len(tool_calls)}", flush=True)
        log.info("agent step=%d ← openai in %.2fs (tool_calls=%d)",
                 step, time.monotonic() - t0, len(tool_calls))

        # Surface the model's free-text thought (if any) before the tool
        # calls. This is what the UI shows under "step N".
        thought = (msg.content or "").strip()
        yield AgentEvent(kind="step", data={
            "step": step,
            "thought": thought,
            "tool_calls": [{"name": tc.function.name,
                            "args": _safe_loads(tc.function.arguments)}
                           for tc in tool_calls],
        })

        # No tool calls → model is done reasoning, drop into synth.
        if not tool_calls:
            finish_reason = "model_stopped"
            break

        # Append the assistant message (with tool_calls) to the convo.
        # The OpenAI API requires the original tool_calls structure here —
        # serialize it back to the dict shape.
        convo.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name,
                                 "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ],
        })

        # Dispatch tool calls in parallel. Two passes so the UI sees
        # each "tool_call" event BEFORE the tool runs (not after all of
        # them finish): pass 1 yields tool_call events + kicks off tasks,
        # pass 2 awaits results as they arrive and yields tool_result.
        finish_called = False
        parsed_calls: list[tuple[Any, dict]] = []
        tasks: list[asyncio.Task] = []
        for tc in tool_calls:
            args = _safe_loads(tc.function.arguments)
            parsed_calls.append((tc, args))
            yield AgentEvent(kind="tool_call", data={
                "step": step, "tool_call_id": tc.id,
                "tool": tc.function.name, "args": args,
            })
            tasks.append(asyncio.create_task(_dispatch_tool(
                name=tc.function.name, args=args,
                store=store, client=client,
                embed_model=embed_model, doc_ids=doc_ids,
                embed_client=embed_client, role=role,
                ops_scope=ops_scope,
            )))

        # Await all results, then emit tool_result events in declaration
        # order (matches the order the model picked them — easier for the
        # UI than as-completed ordering, which races).
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (tc, _args), result in zip(parsed_calls, results):
            if isinstance(result, STORE_UNAVAILABLE_ERRORS):
                raise result  # store waking — abort the turn, see _dispatch_tool
            if isinstance(result, BaseException):
                log.warning("tool %s raised: %s", tc.function.name, result)
                result = {"error": f"{type(result).__name__}: {result}"}

            new_citations: list[Citation] = []
            if (tc.function.name == "aggregate_ops_fields"
                  and isinstance(result, dict) and "error" not in result):
                new_citations = list(result.get("sample") or [])
                aggregations.append({
                    "label": f"OpsField:{result.get('field_key')}",
                    "filters": ({"group": result["group"]} if result.get("group") else {}),
                    "count": int(result.get("n_numbers") or 0),
                    "op": result.get("op"),
                    "prop": "numbers",
                    "value": result.get("value"),
                    "group_by": None,
                    "groups": [{"key": i["doc"], "count": len(i["numbers"] or []),
                                "value": (i["numbers"][0] if i.get("numbers") else None)}
                               for i in (result.get("items") or [])[:10]],
                })
            elif tc.function.name == "finish":
                finish_called = True
            elif isinstance(result, list):
                new_citations = result

            # Redact BEFORE this reaches the bundle, the conversation, or the
            # model's next turn. See the doc_tokens paragraph on run()'s
            # docstring for why this can't wait until the loop is over.
            if doc_tokens is not None and new_citations:
                policy_mod.tag_citations(new_citations, role, doc_tokens=doc_tokens)
                for c in new_citations:
                    if c.restricted:
                        c.snippet = "[restricted for your access level]"
                        c.fact_summary = None
                        c.row_context = None
                        c.linked_context = None
                        c.verified_by = None

            added = _merge_citations(bundle, new_citations)

            # Record this call's ranking for RRF (full tool order, incl. spans
            # already in the bundle — cross-retriever agreement is the signal).
            ranked_ids = [c.evidence_id for c in new_citations if c.evidence_id]
            if ranked_ids:
                retrieval_lists.append(ranked_ids)
                if tc.function.name in _AUTHORITATIVE_TOOLS:
                    privileged_ids.update(ranked_ids)

            log.info("agent step=%d tool=%s n_results=%d n_new=%d total=%d",
                     step, tc.function.name, len(new_citations),
                     len(added), len(bundle))
            yield AgentEvent(kind="tool_result", data={
                "step": step,
                "tool_call_id": tc.id,
                "tool": tc.function.name,
                "n_results": len(new_citations),
                "n_new": len(added),
                "n_total": len(bundle),
                "count": (result.get("count") if isinstance(result, dict)
                          and "count" in result else None),
                "error": (result.get("error") if isinstance(result, dict)
                          and "error" in result else None),
            })

            convo.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": serialize_tool_result(tc.function.name, result, doc_titles),
            })

        if finish_called:
            finish_reason = "finish_tool"
            break

    # Final ordering = RRF over the per-retriever rankings, with authoritative
    # graph facts held on top. Replaces sorting by a score that mixed cosine,
    # BM25, and fixed typed values (incomparable). Tie-break on raw score.
    rrf = _rrf_scores(retrieval_lists)
    citations = sorted(
        bundle.values(),
        key=lambda c: (c.evidence_id in privileged_ids,
                       rrf.get(c.evidence_id, 0.0), c.score),
        reverse=True,
    )
    log.info("agent done: steps=%d reason=%s citations=%d aggregations=%d",
             step, finish_reason, len(citations), len(aggregations))
    yield AgentResult(
        citations=citations,
        steps_used=step,
        finish_reason=finish_reason,
        transcript=convo,
        aggregations=aggregations,
    )


def _safe_loads(s: str | None) -> dict:
    """Tolerant JSON parser — OpenAI tool_call arguments are stringified
    JSON, occasionally with trailing whitespace or empty payloads."""
    if not s:
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {"_raw": s}
