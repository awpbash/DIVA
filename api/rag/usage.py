"""Per-request OpenAI token accounting.

A single ``TokenMeter`` is threaded through the planner, the agent loop, and
synth for one chat request. Each OpenAI call adds its ``response.usage`` under a
stage name, so the route can emit an exact prompt / completion / total token
breakdown (and the eval harness can record cost per case). Read-only: it never
changes what the pipeline does, only measures it.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TokenMeter:
    """Accumulates token usage per pipeline stage for one request."""
    stages: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, stage: str, usage) -> None:
        """Fold one OpenAI ``usage`` object into ``stage``. None-safe (a model or
        a stream that returns no usage simply contributes nothing)."""
        if usage is None:
            return
        s = self.stages.setdefault(
            stage, {"prompt_tokens": 0, "completion_tokens": 0,
                    "total_tokens": 0, "calls": 0})
        s["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
        s["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)
        s["total_tokens"] += int(getattr(usage, "total_tokens", 0) or 0)
        s["calls"] += 1

    def totals(self) -> dict[str, int]:
        out = {"prompt_tokens": 0, "completion_tokens": 0,
               "total_tokens": 0, "calls": 0}
        for s in self.stages.values():
            for k in out:
                out[k] += s[k]
        return out

    def as_dict(self) -> dict:
        return {"by_stage": self.stages, "totals": self.totals()}
