"""Synth pass — streaming answer with citation tags.

Wraps an OpenAI streamed chat completion. Yields text deltas. The route
layer wraps each delta into an SSE event.
"""
from __future__ import annotations

from typing import AsyncIterator, Iterable

from openai import AsyncOpenAI

from .citations import forward_tokens
from .prompts import synth_system_prompt
from .schemas import ChatMessage, Citation, IntentPlan


def _format_aggregations(aggs: list[dict]) -> str:
    """Render aggregation results (true counts) as a banner the model
    cannot miss. The model has a tendency to count the number of evidence
    entries shown instead of using the true total — this preamble exists
    to override that tendency, so it leads with an unambiguous directive."""
    if not aggs:
        return ""
    rows = []
    for a in aggs:
        label = a.get("label") or "?"
        filters = a.get("filters") or {}
        count = a.get("count", 0)
        filt = (" matching " + ", ".join(f"{k}={v}" for k, v in filters.items())
                if filters else "")
        rows.append(f"  • THE TOTAL NUMBER OF {label}{filt} IS {count}.")
        # aggregate_facts results additionally carry a numeric aggregate
        # and an optional per-group breakdown.
        if a.get("prop") and a.get("value") is not None:
            rows.append(f"  • THE {str(a.get('op') or 'sum').upper()} OF "
                        f"{label}.{a['prop']}{filt} IS {a['value']}.")
        for g in a.get("groups") or []:
            val = (f", {a.get('op') or 'sum'}={g.get('value')}"
                   if g.get("value") is not None else "")
            rows.append(f"      - {g.get('key')}: count={g.get('count')}{val}")
    rule = ("⚠ AUTHORITATIVE COUNTS — read this BEFORE anything else. "
            "If the user asks 'how many X' or 'count of X', the answer is "
            "the number on the line below. The evidence list further down "
            "shows only up to 5 sample entries; the sample size is NOT the "
            "answer to a count question.\n")
    return "════════ AGGREGATE TOTALS ════════\n" + rule + "\n".join(rows) + "\n════════════════════════════════════\n\n"


def _format_evidence_block(citations: list[Citation],
                           aggregations: list[dict] | None = None,
                           doc_titles: dict[str, str] | None = None) -> str:
    """Render the evidence bundle for the synth prompt.

    Layout (typed FACT line first, then page/section context, then the
    verbatim snippet). Putting the fact_summary above the snippet makes
    structured attributes — like a Party's role + address — visible
    before the LLM commits to picking a snippet for the answer.

    If aggregations were collected, they're rendered as a TOTALS preamble
    so 'how many X' answers use the true database count, not the sample.
    """
    preamble = _format_aggregations(aggregations or [])
    if not citations:
        return preamble + "(no evidence retrieved — answer must say so)"
    lines: list[str] = []
    for c in citations:
        head = f"[ev:{c.evidence_id}]"
        # Document identity per evidence — without it synth cannot tell a
        # wrong-contract snippet from the right one and mis-attributes it.
        doc_line = ""
        doc_name = (doc_titles or {}).get(c.doc_id)
        if doc_name:
            doc_line = f"  DOC: {doc_name}\n"
        fact_line = ""
        if c.fact_label and c.fact_summary:
            fact_line = f"  FACT: {c.fact_label} — {c.fact_summary}\n"
        elif c.fact_label:
            fact_line = f"  FACT: {c.fact_label}\n"
        source_line = ""
        if c.citation_kind != "evidence_span" or c.confidence_tier in {"raw", "layout"}:
            bits = [c.citation_kind]
            if c.confidence_tier:
                bits.append(f"tier={c.confidence_tier}")
            if c.raw_label:
                bits.append(f"raw_label={c.raw_label}")
            source_line = "  SOURCE: " + " | ".join(bits) + "\n"
        loc_parts: list[str] = []
        if c.section_num:
            loc_parts.append(f"§{c.section_num}")
        if c.section_title:
            loc_parts.append(c.section_title)
        loc_parts.append(f"page {c.page_no}")
        loc_line = "  LOCATION: " + " · ".join(loc_parts) + "\n"
        snippet = (c.snippet or "").strip()
        # Cap each snippet so the prompt doesn't explode on a single long
        # passage — synth has the FACT line + section anchor to reason from.
        if len(snippet) > 800:
            snippet = snippet[:800] + "…"
        snip_line = f"  SNIPPET: {snippet}"
        # Table-row context: the full row the snippet was found in. Carries
        # the condition/tier wording a bare cell fragment loses (e.g. which
        # of two near-identical formulas applies in which window).
        row_line = ""
        if c.row_context and c.row_context.strip() != snippet:
            row_line = f"\n  ROW: {c.row_context.strip()[:600]}"
        # Graph-linked context: conditions (IF:), trigger events (TRIGGER:)
        # and defined terms (TERM ...) connected to this fact by derived
        # edges. Traversed from the graph — present even when the linked
        # fact's own chunk didn't make the retrieval cut.
        linked_line = ""
        if c.linked_context:
            linked = c.linked_context.strip()[:700]
            linked_line = "\n  " + linked.replace("\n", "\n  ")
        lines.append(
            f"{head}\n{doc_line}{source_line}{fact_line}{loc_line}{snip_line}"
            f"{row_line}{linked_line}"
        )
    return preamble + "\n\n".join(lines)


def _verified_swap_map(citations: list[Citation]) -> dict[str, tuple[str, str]]:
    """{raw citation id: (verified ops citation id, lowercased value)}.

    A value a human has verified should carry its consensus chip wherever the
    answer states it — even when the model cites a raw clause instead of the
    knowledge-base record. Only same-document matches where the raw snippet
    contains the verified value qualify; the forwarder additionally checks the
    value appears in the claim text before swapping. Values shorter than 4
    chars are skipped (a bare "7" would match nearly anything)."""
    verified = [(v, v.field_value.strip().lower()) for v in citations
                if v.field_trust == "human_validated" and v.field_value
                and len(v.field_value.strip()) >= 4]
    if not verified:
        return {}
    swap: dict[str, tuple[str, str]] = {}
    for c in citations:
        if c.field_trust:                 # already a verified/disputed record
            continue
        hay = " ".join(filter(None, (c.snippet, c.row_context))).lower()
        best: tuple[str, str] | None = None
        for v, val in verified:
            if v.doc_id != c.doc_id or val not in hay:
                continue
            if best is None or len(val) > len(best[1]):
                best = (v.evidence_id, val)   # most specific value wins
        if best:
            swap[c.evidence_id] = best
    return swap


def _rescue_index(citations: list[Citation]) -> list[tuple[str, str, int]]:
    """`[(value_lower, evidence_id, rank)]` — a value→real-citation index the
    streaming gate uses to rescue a dropped tag. The model, faced with several
    near-identical fields (customer_name across a document family, a sibling
    supplier field it invents by analogy), sometimes cites an id that wasn't
    retrieved; that tag would otherwise be stripped and the claim left uncited.

    Two clean, low-noise value sources: human-verified field values (rank 0, so
    a rescued value keeps its consensus chip) and canonical party names read off
    the ``NAME · role · address`` summary. AI-extracted raw field values are
    deliberately excluded — the party names cover the same strings without the
    junk ("COMPANY", "Supplier; Customer") a placeholder field would inject."""
    idx: list[tuple[str, str, int]] = []
    for c in citations:
        if (c.field_trust == "human_validated" and c.field_value
                and len(c.field_value.strip()) >= 4):
            idx.append((c.field_value.strip().lower(), c.evidence_id, 0))
        summ = c.fact_summary or ""
        if " · " in summ and not summ.startswith("["):
            head = summ.split(" · ")[0].strip().lower()
            if len(head) >= 4:
                idx.append((head, c.evidence_id, 1))
    # Verified first, then a stable id order so ties resolve deterministically
    # (evidence_ids sort span e1 before e2 → the page-1 mention wins).
    idx.sort(key=lambda t: (t[2], t[1]))
    return idx


def _format_history(messages: Iterable[ChatMessage]) -> str:
    items = [m for m in messages if m.role in ("user", "assistant")]
    if not items:
        return "(this is the first turn)"
    return "\n".join(f"{m.role}: {m.content}" for m in items)


async def _openai_stream(
    client: AsyncOpenAI, model: str, system_prompt: str, user_question: str,
    max_tokens: int, meter=None,
) -> AsyncIterator[str]:
    stream = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_question},
        ],
        max_completion_tokens=max_tokens,
        stream=True,
        stream_options={"include_usage": True},
    )
    async for chunk in stream:
        # The final usage chunk carries no choices — capture it for the meter.
        if meter is not None and getattr(chunk, "usage", None):
            meter.add("synth", chunk.usage)
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta and delta.content:
            yield delta.content


async def synth_stream(
    *, client: AsyncOpenAI, model: str,
    plan: IntentPlan, citations: list[Citation],
    messages: list[ChatMessage], max_tokens: int = 4000,
    aggregations: list[dict] | None = None,
    catalog: str = "", meter=None,
    doc_titles: dict[str, str] | None = None,
    scope_docs_block: str = "",
    redaction_note: str = "",
) -> AsyncIterator[str]:
    """Yield text deltas. Caller wraps into SSE."""
    if not messages:
        return
    user_question = messages[-1].content
    history = _format_history(messages[:-1])
    evidence_block = _format_evidence_block(citations, aggregations, doc_titles)
    system_prompt = synth_system_prompt(
        intent=plan.intent,
        needs_table=plan.needs_table,
        evidence_block=evidence_block,
        history_block=history,
        catalog_block=catalog,
        scope_docs_block=scope_docs_block,
        redaction_note=redaction_note,
    )
    upstream = _openai_stream(
        client, model, system_prompt, user_question, max_tokens, meter,
    )
    # Deterministic guard: only let citations that are actually in the
    # retrieved bundle through. Fabricated ids (e.g. the unknown:unknown
    # synth invents for aggregate counts) are stripped, not shown. The swap
    # map additionally upgrades raw-clause citations to the human-verified
    # knowledge-base record when the answer states a verified value.
    allowed_ids = {c.evidence_id for c in citations if c.evidence_id}
    async for piece in forward_tokens(upstream, allowed_ids,
                                      swap=_verified_swap_map(citations),
                                      rescue=_rescue_index(citations)):
        yield piece
