"""
token_meter.py — process-wide LLM token accounting for one ingest run.

Every real OpenAI call site reports its ``usage`` here, tagged by pipeline
stage. ``ingest_doc`` resets the meter per document, then writes the per-stage
breakdown to ``runs/<doc_id>/token_usage.json`` and prints it — so the token
cost of a run is visible per pipeline part (which matters on a tight budget).

Stages with their own LLM caller pass ``stage=`` explicitly. ``understand``
shares one caller across outline / harvest / normalise, so it wraps each pass
in ``with token_meter.using("<substage>")`` and the caller reads the contextvar.

Concurrency: the pipeline is single-threaded asyncio. ``record`` is a synchronous
dict update (atomic between awaits), so the per-category / per-unit fan-outs
don't need a lock. ``using`` sets a contextvar, which child tasks created inside
the block inherit — so a normalise call inside its gather is still tagged.
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import asdict, dataclass


@dataclass
class _Bucket:
    n_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


# stage -> model -> bucket
_LEDGER: dict[str, dict[str, _Bucket]] = {}
_STAGE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "token_meter_stage", default="unknown",
)

# Print order = pipeline order; anything unlisted sorts after, alphabetically.
_ORDER = ["correct_classify", "table_repair", "outline", "harvest",
          "unit_extract", "normalise", "repair", "embed"]


def reset() -> None:
    """Clear the ledger. Called at the start of each document's ingest."""
    _LEDGER.clear()


@contextlib.contextmanager
def using(stage: str):
    """Tag every ``record(...)`` made inside this block (and inside any asyncio
    task created within it) with ``stage`` — for callers shared across
    sub-stages (understand's outline / harvest / normalise)."""
    token = _STAGE.set(stage)
    try:
        yield
    finally:
        _STAGE.reset(token)


def record(usage, *, stage: str | None = None, model: str = "unknown") -> None:
    """Add one LLM call's token usage. ``usage`` is an OpenAI usage object
    (chat or embeddings) or None — a cache hit / injected fake reports nothing.
    ``stage`` defaults to the ``using(...)`` contextvar. Embeddings have no
    ``completion_tokens`` (defaults to 0)."""
    if usage is None:
        return
    s = stage if stage is not None else _STAGE.get()
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or (prompt + completion))
    b = _LEDGER.setdefault(s, {}).setdefault(model, _Bucket())
    b.n_calls += 1
    b.prompt_tokens += prompt
    b.completion_tokens += completion
    b.total_tokens += total


def snapshot() -> dict:
    """``{"by_stage": {stage: {model: {...}}}, "total": {...}}`` — JSON-ready."""
    by_stage: dict[str, dict[str, dict]] = {}
    grand = _Bucket()
    for stage, by_model in _LEDGER.items():
        by_stage[stage] = {}
        for model, b in by_model.items():
            by_stage[stage][model] = asdict(b)
            grand.n_calls += b.n_calls
            grand.prompt_tokens += b.prompt_tokens
            grand.completion_tokens += b.completion_tokens
            grand.total_tokens += b.total_tokens
    return {"by_stage": by_stage, "total": asdict(grand)}


def report() -> str:
    """Human-readable per-stage table, in pipeline order."""
    snap = snapshot()
    rows = snap["by_stage"]
    head = (f"{'stage':<16}{'model':<20}{'calls':>6}"
            f"{'prompt':>11}{'compl':>10}{'total':>11}")
    lines = [head, "-" * len(head)]

    def _key(s: str) -> tuple[int, str]:
        return (_ORDER.index(s) if s in _ORDER else len(_ORDER), s)

    for stage in sorted(rows, key=_key):
        for model, b in rows[stage].items():
            lines.append(
                f"{stage:<16}{model:<20}{b['n_calls']:>6}"
                f"{b['prompt_tokens']:>11}{b['completion_tokens']:>10}"
                f"{b['total_tokens']:>11}"
            )
    g = snap["total"]
    lines.append("-" * len(head))
    lines.append(
        f"{'TOTAL':<16}{'':<20}{g['n_calls']:>6}"
        f"{g['prompt_tokens']:>11}{g['completion_tokens']:>10}{g['total_tokens']:>11}"
    )
    return "\n".join(lines)
