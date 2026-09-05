"""Chat endpoint — SSE stream of plan, agent steps, citations, tokens.

Wire format (events arrive in this order)::

    event: plan
    data: {"intent": "...", "key_terms": [...], ...}

    # Repeats per agent loop iteration:
    event: step
    data: {"step": 1, "thought": "...", "tool_calls": [{"name", "args"}]}

    event: tool_call
    data: {"step": 1, "tool_call_id": "...", "tool": "...", "args": {...}}

    event: tool_result
    data: {"step": 1, "tool_call_id": "...", "tool": "...",
           "n_results": N, "n_new": N, "n_total": N, "count": N|null,
           "error": "..."|null}

    # After loop terminates:
    event: agent_done
    data: {"steps_used": N, "finish_reason": "model_stopped|finish_tool|max_steps"}

    event: citations
    data: {"citations": [...]}

    event: token
    data: {"delta": "..."}
    # ... many token events ...

    event: done
    data: {"used_citations": [...], "unknown_citations": [...]}

On error:

    event: error
    data: {"message": "..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from pipeline.store.aio import STORE_UNAVAILABLE_ERRORS
from sse_starlette.sse import EventSourceResponse

from .. import appdb, deps, ratelimit
from .. import trust as trust_mod
from ..rag import agent, planner, synth
from ..rag import resolve as resolve_mod
from ..rag import policy as policy_mod
from ..rag.agent import AgentEvent, AgentResult
from ..rag.citations import extract_cited_ids
from ..rag.usage import TokenMeter
from ..rag.schemas import ChatRequest
from ..rag.tools import (
    build_catalog_text,
    build_doc_titles,
    build_ops_scope,
    build_scope_docs_block,
)
from ..settings import get_config, get_settings
from .auth import current_user


router = APIRouter(tags=["chat"])
log = logging.getLogger("rag.chat")


def _sse(event: str, data: dict) -> dict:
    """sse-starlette envelope. Names match the wire format above."""
    return {"event": event, "data": json.dumps(data, ensure_ascii=False)}


# Phrases the synth prompt's own redaction rule explicitly forbids (see
# api/rag/prompts.py rule 10), checked here as a deterministic backstop. The
# all-hidden short-circuit further down only fires when EVERY relevant fact
# was withheld. When only SOME was, synth still runs normally over what IS
# visible and can legitimately conclude, in good faith, that a value it never
# saw "is not stated". The prompt rule is an instruction, not a guarantee, so
# this catches the case the instruction doesn't.
_ABSENCE_PHRASES = (
    "not stated", "does not state", "not in the documents",
    "not available", "no evidence was retrieved", "is not mentioned",
    "isn't stated",
)


def _looks_like_absence_claim(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _ABSENCE_PHRASES)


def _effective_role(user: dict, requested: str | None) -> str:
    """The role every filter in this request runs under. ALWAYS the session
    account's role — except an ADMIN may impersonate another role via the
    request field (debugging / evals: "answer this as a default user would
    see it"). A non-admin's requested role is ignored, never honoured."""
    if requested and user.get("role") == "admin":
        return requested
    return str(user.get("role") or policy_mod.default_role())


async def _conf_value_tokens(doc_ids: list[str]) -> dict[str, list[str]]:
    """{doc_id: confidential value tokens} for the bundle's documents — the
    answer-time value net (stamped by build_km). Best-effort: an empty map just
    means the label-class check stands alone this request."""
    if not doc_ids:
        return {}
    try:
        store = deps.get_store()
        rows = await store.query(
            "SELECT c.doc_id, c.conf_tokens FROM c WHERE c.kind = 'document' "
            "AND ARRAY_CONTAINS(@ids, c.doc_id)",
            [{"name": "@ids", "value": doc_ids}])
        return {r["doc_id"]: list(r.get("conf_tokens") or []) for r in rows}
    except Exception:  # noqa: BLE001 — never fail the answer over the net fetch
        return {}


async def _stream(req: ChatRequest, role: str, email: str = "") -> AsyncIterator[dict]:
    cfg = get_config()
    settings = get_settings()
    store = deps.get_store()
    client = deps.get_openai()
    embed_client = deps.get_openai_embed()

    planner_model = settings.planner_model_override or cfg.text_model
    agent_model = settings.agent_model_override or cfg.text_model
    synth_model = settings.synth_model_override or cfg.text_model
    meter = TokenMeter()

    def _usage_event() -> dict:
        return _sse("usage", {**meter.as_dict(), "models": {
            "planner": planner_model, "agent": agent_model, "synth": synth_model}})

    t_chat = time.monotonic()
    question = req.messages[-1].content if req.messages else ""
    print(f"[chat] START q={question[:120]!r} doc_ids={req.doc_ids}", flush=True)
    log.info("chat START q=%r doc_ids=%s", question[:120], req.doc_ids)

    try:
        # 1. Plan — classify intent; seeds the agent's system prompt.
        t = time.monotonic()
        print(f"[chat] planner → openai (model={planner_model})", flush=True)
        plan = await planner.plan(client, planner_model, req.messages, meter=meter)
        print(f"[chat] planner ← {time.monotonic()-t:.2f}s "
              f"intent={plan.intent} key_terms={plan.key_terms}", flush=True)
        log.info("plan done in %.2fs: intent=%s key_terms=%s",
                 time.monotonic() - t, plan.intent, plan.key_terms)
        yield _sse("plan", plan.model_dump())

        # 1b. Scope — resolve the site/party named in the question to its
        # contract family, so retrieval stays in the RIGHT document(s) instead
        # of mixing near-identical template-twins. Explicit user selection
        # always wins; otherwise the resolver narrows; no match => whole corpus.
        scope = await resolve_mod.resolve_scope(store, question, plan.key_terms)
        eff_doc_ids = req.doc_ids or (scope.doc_ids or None)
        # Unknown-name warning: the question apparently NAMED something and
        # resolution matched zero documents. The note steers agent + synth
        # away from borrowing template-twin clauses and offers the closest
        # known names as "did you mean". Empty when resolution succeeded,
        # when the user pinned documents, or when nothing was named.
        scope_note = ("" if req.doc_ids
                      else resolve_mod.unresolved_scope_note(scope))
        yield _sse("scope", {
            "doc_ids": eff_doc_ids or [],
            "source": ("user" if req.doc_ids else ("resolver" if scope.doc_ids else "none")),
            "matches": [{"entity": m.entity, "kind": m.kind, "score": m.score}
                        for m in scope.matches],
            "reason": scope.reason,
            "near_misses": scope.near_misses,
        })
        print(f"[chat] scope: {len(eff_doc_ids or [])} doc(s) — {scope.reason}", flush=True)

        # OPS-layer family expansion for user-pinned docs, computed once per
        # request: a pinned amendment inherits current rows from unpinned
        # family siblings (marked carried-over in the tools). Fact and
        # raw-text tools stay strictly pinned via eff_doc_ids.
        ops_scope = (await build_ops_scope(store, req.doc_ids)
                     if req.doc_ids else None)

        # Corpus catalog — one cheap query; goes to both the agent (so it
        # knows what exists and covers every relevant doc) and synth (so
        # answers name documents instead of bare doc ids).
        catalog = await build_catalog_text(store, None)
        # Document identity (full map so any cited doc resolves) + the resolved
        # family rendered as a citable, linkable list for doc-link answers.
        doc_titles = await build_doc_titles(store, None)
        scope_docs_block = await build_scope_docs_block(store, eff_doc_ids)
        if scope_note:
            # Synth reads the note under "Documents in scope" (empty when
            # unresolved, so the warning stands alone there).
            scope_docs_block = (scope_docs_block + "\n\n" + scope_note).strip()

        # Computed upfront, before the agent loop, not after it: the value net
        # (a document's confidential text tokens, stamped by build_km) is the
        # belt-and-braces catch for a fact that slips past the per-tool source
        # filters. Computing it only after the loop finished, the way this
        # used to work, meant that catch only ever fired on the OUTGOING
        # citation bundle, after the model had already read the unredacted
        # tool result and narrated it into a "step" event that streams to the
        # browser immediately, well before any post-loop filter runs. Passing
        # it into the loop closes that gap: see agent.py's run() for where it
        # gets applied per tool call, before a result ever reaches the model.
        doc_tokens = ({} if not policy_mod.denied_classes(role) else
                      await _conf_value_tokens(sorted(eff_doc_ids or doc_titles.keys())))

        # 2. Agent loop — tool-calling retrieval. Emits step / tool_call /
        # tool_result events; returns an AgentResult at the end.
        citations = []
        aggregations: list[dict] = []
        async for ev in agent.run(
            client=client, model=agent_model, store=store,
            embed_model=cfg.embed_model, plan=plan, messages=req.messages,
            doc_ids=eff_doc_ids, max_steps=settings.agent_max_steps,
            catalog=catalog, meter=meter, doc_titles=doc_titles,
            embed_client=embed_client,
            # Source-filtering tools (verified KB, raw-text search) run under
            # the session role resolved by the route.
            role=role, doc_tokens=doc_tokens,
            ops_scope=ops_scope, scope_note=scope_note,
        ):
            if isinstance(ev, AgentEvent):
                yield _sse(ev.kind, ev.data)
            elif isinstance(ev, AgentResult):
                citations = ev.citations
                aggregations = ev.aggregations
                yield _sse("agent_done", {
                    "steps_used": ev.steps_used,
                    "finish_reason": ev.finish_reason,
                    "n_citations": len(citations),
                })

        # Access policy (RBAC redaction). Every citation was already tagged
        # (sensitivity + restricted stamped) inside the agent loop above, one
        # tool call at a time, not re-tagged here: tag_citations' value-net
        # check reads a citation's OWN snippet/fact_summary text to catch a
        # confidential value that slipped past the label check, and the loop
        # already blanked those same fields the moment it found a match. A
        # second pass over the now-redacted text would search empty ground
        # and could un-flag exactly the citation this exists to catch. What's
        # left here is building the answer from VISIBLE evidence only.
        # Restricted facts never reach synth, secure by construction.
        visible, hidden = policy_mod.partition(citations, role)
        # Compliance trail, the other half of redaction. `hidden` already
        # proves a blocked attempt never reaches the answer. This records the
        # opposite case, a CLEARED role's answer actually drawing on
        # confidential material, so "who saw this field and when" has a real
        # answer instead of only "who was blocked". Lands in the same audit
        # log the admin Activity tab already reads.
        exposed = [c for c in visible if c.sensitivity]
        if exposed and email:
            by_class: dict[str, int] = {}
            for c in exposed:
                by_class[c.sensitivity] = by_class.get(c.sensitivity, 0) + 1
            # to_thread: appdb's sqlite calls are synchronous, and this route
            # runs on the shared event loop, so a call made directly here
            # would freeze every other request for however long the write
            # takes. See api/appdb.py's _conn() for the full reasoning.
            await asyncio.to_thread(
                appdb.log_event,
                email, "confidential_access", "viewed confidential field(s)",
                target=", ".join(sorted({c.doc_id for c in exposed})),
                detail=", ".join(f"{n} {cls}" for cls, n in sorted(by_class.items())))
        redaction_note = policy_mod.redaction_note(hidden, role)
        restricted_labels = policy_mod.restricted_labels(role)
        if restricted_labels:
            # Drop aggregations over restricted labels so a sum/avg can't leak a total.
            aggregations = [a for a in aggregations
                            if a.get("label") not in restricted_labels]
        print(f"[chat] role={role} hidden={len(hidden)} of {len(citations)}", flush=True)

        # Full bundle (tagged) goes to the client so the UI can blur restricted
        # evidence; the answer below is synthesised from `visible` only.
        # Each citation carries its document's review-trust tier (reviewed /
        # partial / unreviewed) so the reader sees, at the point of reference,
        # whether a human has stood behind the cited document.
        doc_trust = await trust_mod.review_status_async()
        # Verifier display names for the badge tooltip ("Verified by Alias Bin
        # Othman…"): one accounts read per answer, emails fall back to themselves.
        # to_thread for the same reason as the log_event call above.
        verifier_names = {}
        if any(c.verified_by for c in citations):
            users = await asyncio.to_thread(appdb.list_users)
            verifier_names = {u["email"]: (u.get("name") or u["email"]) for u in users}
        cite_dicts = []
        for c in citations:
            d = c.model_dump()
            if c.restricted:
                # Server-side redaction: the tagged bundle keeps ids + geometry
                # so the client can draw the blur box, but restricted TEXT never
                # rides along in the payload — dev-tools would read it straight
                # off the wire. Mirrors /evidence's direct-fetch withholding.
                d["snippet"] = "[restricted for your access level]"
                d["fact_summary"] = None
                d["row_context"] = None
                d["linked_context"] = None
                d["verified_by"] = None
            elif c.verified_by:
                d["verified_by"] = [verifier_names.get(v, v) for v in c.verified_by]
            t = doc_trust.get(c.doc_id)
            d["doc_trust"] = t["tier"] if t else "unreviewed"
            d["doc_verified"] = t["verified"] if t else 0
            d["doc_populated"] = t["populated"] if t else 0
            cite_dicts.append(d)
        yield _sse("citations", {
            "citations": cite_dicts,
            "role": role, "hidden": len(hidden),
        })

        # Retrieval-eval path: the bundle is what we want to score — stop here,
        # before the (expensive) synth pass. Emit a `done` with no answer so
        # the client sees a clean end-of-stream.
        if req.retrieval_only:
            log.info("chat END (retrieval_only) total=%.2fs n_citations=%d",
                     time.monotonic() - t_chat, len(citations))
            yield _usage_event()
            yield _sse("done", {"used_citations": [], "unknown_citations": [],
                                "retrieval_only": True})
            return

        # All-restricted short-circuit: every retrieved fact was withheld for
        # this role, so there is NOTHING visible to synthesise. Skipping synth
        # here avoids (a) a wasted token spend on a doomed call and (b) the
        # model defaulting to "no evidence / not in the documents" — a trust
        # error, because the data EXISTS, the viewer just isn't cleared for it.
        # The tagged bundle was already emitted above, so the UI still shows the
        # redaction strip + blurred chips alongside this deterministic notice.
        if not visible and not aggregations and hidden:
            classes = sorted({policy_mod.sensitivity_of(c) or "restricted" for c in hidden})
            cls_phrase = " / ".join(classes)
            n = len(hidden)
            msg = (
                f"🔒 This is **restricted for your access level**. "
                f"{n} {cls_phrase} item{'s' if n != 1 else ''} matching your "
                f"question {'are' if n != 1 else 'is'} present in the documents "
                f"but withheld for the current role (**{role}**). Switch to a "
                f"cleared role (Full view) to see {'them' if n != 1 else 'it'}."
            )
            yield _sse("token", {"delta": msg})
            log.info("chat END (all-restricted) total=%.2fs hidden=%d",
                     time.monotonic() - t_chat, n)
            yield _usage_event()
            yield _sse("done", {"used_citations": [], "unknown_citations": []})
            return

        allowed_ids = {c.evidence_id for c in visible}
        full_text_parts: list[str] = []

        # 3. Synth — streamed answer over the VISIBLE evidence only.
        t_synth = time.monotonic()
        log.info("synth start: n_visible=%d n_hidden=%d", len(visible), len(hidden))
        async for delta in synth.synth_stream(
            client=client, model=synth_model, plan=plan,
            citations=visible, messages=req.messages,
            aggregations=aggregations,
            max_tokens=settings.max_synth_tokens,
            catalog=catalog, meter=meter,
            doc_titles=doc_titles, scope_docs_block=scope_docs_block,
            redaction_note=redaction_note,
        ):
            full_text_parts.append(delta)
            yield _sse("token", {"delta": delta})

        full_text = "".join(full_text_parts)
        # Deterministic backstop for the PARTIAL-redaction case (some but not
        # all relevant evidence was hidden): the model was told never to
        # phrase a hidden value as absent, but that's an instruction, not a
        # guarantee, and it can slip. It can only be appended, not rewritten
        # in place, since the flawed sentence has already streamed to the
        # browser token by token.
        if redaction_note and _looks_like_absence_claim(full_text):
            correction = (
                "\n\n⚠ Note: some evidence for this question was withheld for "
                "your access level. If anything above reads as though a value "
                "is not stated, it may instead be restricted rather than "
                "genuinely absent.")
            full_text += correction
            yield _sse("token", {"delta": correction})
        used = set(extract_cited_ids(full_text))
        unknown = sorted(used - allowed_ids)
        log.info("synth done in %.2fs: chars=%d used=%d unknown=%d",
                 time.monotonic() - t_synth, len(full_text),
                 len(used), len(unknown))
        log.info("chat END total=%.2fs", time.monotonic() - t_chat)
        yield _usage_event()
        yield _sse("done", {
            "used_citations":    sorted(used),
            "unknown_citations": unknown,
        })

    except Exception as exc:  # noqa: BLE001 — one turn failing must not kill the stream
        # The full exception goes to the log with a correlation id. What
        # reaches the browser is a sentence and that id.
        #
        # It used to be `str(exc)`. api/main.py sets the opposite policy for
        # every other route ("never leak stack traces to clients"), and a
        # provider or store error message can carry an endpoint URI or internal
        # detail, so the raw text was going to exactly the wrong audience.
        ref = uuid.uuid4().hex[:8]
        log.exception("chat error [%s]: %s", ref, exc)
        if isinstance(exc, STORE_UNAVAILABLE_ERRORS):
            # Cold start: the knowledge store is still booting. This one is
            # worth naming, because it is expected and it resolves itself.
            msg = ("The knowledge base is still waking up after being idle. "
                   "Give it about half a minute, then ask again.")
        else:
            msg = ("Something went wrong answering that. The details are in "
                   f"the server log under reference {ref}.")
        yield _sse("error", {"message": msg, "ref": ref})
    finally:
        # Usage metering (admin metrics: who asks how much). Runs on normal
        # completion, errors AND client aborts (generator close); rows only
        # when the LLM was actually called, so failed-before-spend turns are
        # free. log_usage never raises.
        totals = meter.totals()
        if email and totals.get("calls"):
            await asyncio.to_thread(
                appdb.log_usage, email, "chat",
                prompt_tokens=totals["prompt_tokens"],
                completion_tokens=totals["completion_tokens"],
                calls=totals["calls"])


@router.post("/chat")
async def chat(req: ChatRequest, user: dict = Depends(current_user)):
    # Every call here makes at least one real, paid model call and often
    # several (plan, agent tool-calling steps, synth), with no other ceiling
    # anywhere in the app on how often one account can trigger that. Capped
    # per account rather than per IP: the cost follows the login, not the
    # network address, and a shared/leaked login is exactly the case this
    # needs to bound. Generous enough for a person typing questions, tight
    # enough to make a scripted loop expensive to run rather than free.
    if not ratelimit.allow(f"chat:{user['email']}", limit=20, window=60.0):
        raise HTTPException(429, "too many questions in a short time. Wait a "
                                 "minute and try again.")
    # Usage is metered under the SESSION account even when an admin
    # impersonates another role — spend follows the person, not the mask.
    return EventSourceResponse(
        _stream(req, _effective_role(user, req.role), email=user["email"]))
