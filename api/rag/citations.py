"""Citation tag parser + validator.

The synth LLM emits ``[ev:<evidence_id>]`` markers inline. This module:

1. **Streams safely**: a tag can be split across token boundaries, so the
   token stream is buffered just long enough to keep tags intact before
   forwarding to the client.

2. **Validates post-hoc**: after streaming completes, parses the full text
   and asserts every ``[ev:...]`` ID was in the supplied bundle. Returns
   the set of unknown IDs so the route can flag the answer as suspect.
"""
from __future__ import annotations

import re
from typing import AsyncIterator


# A loose pattern — IDs can have ':' separators and alphanumerics. Anchored
# to the ``[ev:`` prefix.
_CITE_RE = re.compile(r"\[ev:([A-Za-z0-9_\-:\.]+)\]")


def extract_cited_ids(text: str) -> list[str]:
    return _CITE_RE.findall(text or "")


def _strip_unknown_tags(text: str, allowed: set[str] | None) -> str:
    """Drop ``[ev:id]`` tags whose id isn't in ``allowed`` — fabricated or
    altered citations (e.g. the ``…:unknown:unknown`` ids synth invents for
    aggregate counts, which have no backing span). A citation that resolves
    to nothing is worse than none, so this removes them deterministically
    rather than trusting the prompt. ``allowed=None`` disables stripping."""
    if not allowed:
        return text
    return _CITE_RE.sub(
        lambda m: m.group(0) if m.group(1) in allowed else "", text,
    )


def validate_citations(
    text: str, allowed_ids: set[str]
) -> tuple[set[str], set[str]]:
    """Return ``(used_ids, unknown_ids)``."""
    used = set(extract_cited_ids(text))
    unknown = {i for i in used if i not in allowed_ids}
    return used, unknown


# ---------------------------------------------------------------------------
# Streaming tag-safe forwarder
# ---------------------------------------------------------------------------

# How much recently-streamed answer text a swap looks back through to confirm
# the claim actually states the verified value. Sentence-scale on purpose: a
# clause cited for a DIFFERENT claim (an address, say) must not inherit the
# verified-name consensus just because both live in the same snippet.
_SWAP_WINDOW = 300

# A rescued value must be stated within this many characters of the dropped
# tag — the value has to be part of the immediate claim, not something
# mentioned a sentence or two earlier that happens to sit in the window.
_RESCUE_NEAR = 160


def _pick_rescue(window_lower: str,
                 rescue: list[tuple[str, str, int]] | None) -> str | None:
    """A citation the model emitted for a provable value is about to be dropped
    because its id isn't in the bundle (the model mis-copied or invented it,
    often by analogy to a sibling field). Rather than leave the claim uncited,
    attach a REAL bundle citation that carries the value the claim just stated.

    ``rescue`` is ``[(value_lower, evidence_id, rank)]`` (rank 0 = verified,
    preferred). We pick the value stated CLOSEST to the tag — the claim right
    before it — then longer value, then verified. ``None`` if nothing matches.
    """
    if not rescue:
        return None
    wlen = len(window_lower)
    best: tuple[tuple[int, int, int], str] | None = None
    for val, eid, rank in rescue:
        i = window_lower.rfind(val)
        if i == -1:
            continue
        end = i + len(val)
        if wlen - end > _RESCUE_NEAR:
            continue                      # stated too far back to be this claim
        key = (end, len(val), -rank)
        if best is None or key > best[0]:
            best = (key, eid)
    return best[1] if best else None


def _rewrite_tags(text: str, allowed: set[str] | None,
                  swap: dict[str, tuple[str, str]] | None,
                  rescue: list[tuple[str, str, int]] | None,
                  state: dict) -> str:
    """Process every complete `[ev:id]` tag in ``text``: drop fabricated ids
    (rescuing them to a real same-value citation when possible), and upgrade a
    raw-text citation to its verified knowledge-base record when the swap map
    has one AND the value appears in the recent answer text. ``state`` carries
    the rolling window + last-emitted tag across streamed chunks. Adjacent
    duplicates after a swap/rescue collapse to one tag.
    """
    out: list[str] = []
    pos = 0
    for m in _CITE_RE.finditer(text):
        gap = text[pos:m.start()]
        pos = m.end()
        out.append(gap)
        state["window"] = (state["window"] + gap)[-_SWAP_WINDOW:]
        if gap.strip():
            state["prev_tag"] = None      # real text between tags: new claim
        cid = m.group(1)
        if allowed and cid not in allowed:
            # Fabricated / mis-copied id: rescue to a real citation for the
            # value this claim states, else drop it (an unresolvable tag is
            # worse than none).
            rid = _pick_rescue(state["window"].lower(), rescue)
            if rid is None:
                continue
            cid = rid
        if swap and cid in swap:
            verified_id, value = swap[cid]
            if value in state["window"].lower():
                cid = verified_id
        if cid == state.get("prev_tag"):
            continue                      # [a][b] both swapped to the same id
        out.append(f"[ev:{cid}]")
        state["prev_tag"] = cid
    tail = text[pos:]
    out.append(tail)
    state["window"] = (state["window"] + tail)[-_SWAP_WINDOW:]
    if tail.strip():
        state["prev_tag"] = None
    return "".join(out)


async def forward_tokens(
    upstream: AsyncIterator[str],
    allowed_ids: set[str] | None = None,
    swap: dict[str, tuple[str, str]] | None = None,
    rescue: list[tuple[str, str, int]] | None = None,
) -> AsyncIterator[str]:
    """Yield text chunks but never split an `[ev:...]` tag across yields,
    deterministically drop any `[ev:id]` whose id isn't in ``allowed_ids``
    (so a fabricated citation never reaches the client), and — given a
    ``swap`` map of {raw_id: (verified_id, value)} — replace a raw-text
    citation with its human-verified knowledge-base record whenever the
    surrounding answer text states that value.

    ``rescue`` (``[(value_lower, evidence_id, rank)]``) turns an otherwise
    dropped id into a real citation when the claim states a value some bundle
    citation carries — so a provable fact the model cited with the wrong id
    still shows a working, correctly-attributed chip instead of vanishing.

    We buffer whenever the tail of pending text contains an unclosed `[`
    that might still grow into a full tag. As soon as we see a closing `]`
    OR a character that can't be inside a tag, we flush. Flushed text only
    ever contains COMPLETE tags, so rewriting them there is safe.
    """
    state = {"window": "", "prev_tag": None}

    def emit(s: str) -> str:
        return _rewrite_tags(s, allowed_ids, swap, rescue, state)

    buf = ""
    async for piece in upstream:
        buf += piece
        # Find the last `[` — anything before it is safe to emit.
        last_open = buf.rfind("[")
        if last_open == -1:
            yield emit(buf)
            buf = ""
            continue
        # If the `[` has a matching `]` after it, the tag is complete; flush
        # everything (the tag is whole inside `buf`). Otherwise hold the
        # `[...` tail.
        last_close = buf.find("]", last_open)
        if last_close != -1:
            yield emit(buf)
            buf = ""
        else:
            # Flush the safe prefix and hold the rest.
            if last_open > 0:
                yield emit(buf[:last_open])
                buf = buf[last_open:]
            # else: buf is just an open tag, hold it all
    if buf:
        yield emit(buf)
