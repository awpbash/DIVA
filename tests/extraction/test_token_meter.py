"""Unit tests for token_meter — per-stage LLM token accounting."""
from __future__ import annotations

from pipeline.extraction import token_meter


class _Usage:
    """Stand-in for an OpenAI usage object."""
    def __init__(self, prompt, completion, total=None):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = total if total is not None else prompt + completion


def test_record_and_snapshot_aggregate():
    token_meter.reset()
    token_meter.record(_Usage(10, 5), stage="harvest", model="m1")
    token_meter.record(_Usage(20, 7), stage="harvest", model="m1")
    token_meter.record(_Usage(3, 0), stage="embed", model="e1")   # embedding-like
    snap = token_meter.snapshot()
    h = snap["by_stage"]["harvest"]["m1"]
    assert h == {"n_calls": 2, "prompt_tokens": 30,
                 "completion_tokens": 12, "total_tokens": 42}
    assert snap["total"]["n_calls"] == 3
    assert snap["total"]["total_tokens"] == 42 + 3


def test_none_usage_is_ignored():
    token_meter.reset()
    token_meter.record(None, stage="harvest", model="m1")
    assert token_meter.snapshot()["total"]["n_calls"] == 0


def test_missing_completion_defaults_to_zero():
    """Embeddings usage has no completion_tokens — total falls back to prompt."""
    token_meter.reset()

    class _EmbUsage:
        prompt_tokens = 8
        total_tokens = 8
    token_meter.record(_EmbUsage(), stage="embed", model="e1")
    b = token_meter.snapshot()["by_stage"]["embed"]["e1"]
    assert b["completion_tokens"] == 0 and b["total_tokens"] == 8


def test_using_contextvar_tags_stage_when_not_explicit():
    token_meter.reset()
    with token_meter.using("normalise"):
        token_meter.record(_Usage(5, 5), model="m1")   # no explicit stage
    assert "normalise" in token_meter.snapshot()["by_stage"]


def test_reset_clears():
    token_meter.record(_Usage(1, 1), stage="x", model="m")
    token_meter.reset()
    assert token_meter.snapshot()["total"]["n_calls"] == 0


def test_report_renders_stages_and_total():
    token_meter.reset()
    token_meter.record(_Usage(10, 5), stage="harvest", model="m1")
    r = token_meter.report()
    assert "harvest" in r and "TOTAL" in r
