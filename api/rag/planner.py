"""Intent planner.

Single async LLM call that turns the latest user question (+ history) into
an ``IntentPlan``. Cheap model is fine — the planner's job is shape only,
not answer.
"""
from __future__ import annotations

import json
from typing import Iterable

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from .prompts import planner_system
from .schemas import ChatMessage, IntentPlan


_PLANNER_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "intent_plan",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [
                        "factual", "comparison", "formula", "clause",
                        "cross_ref", "aggregation", "definition", "other",
                    ],
                },
                "key_terms":   {"type": "array", "items": {"type": "string"}},
                "categories":  {"type": "array", "items": {"type": "string"}},
                "needs_table": {"type": "boolean"},
                "rationale":   {"type": "string"},
            },
            "required": ["intent", "key_terms", "categories", "needs_table", "rationale"],
        },
    },
}


def _render_history(messages: Iterable[ChatMessage]) -> str:
    lines: list[str] = []
    for m in messages:
        lines.append(f"{m.role}: {m.content}")
    return "\n".join(lines)


@retry(stop=stop_after_attempt(3),
       wait=wait_exponential(multiplier=1, min=1, max=8),
       reraise=True)
async def plan(
    client: AsyncOpenAI, model: str, messages: list[ChatMessage],
    meter=None,
) -> IntentPlan:
    """Classify the latest user question into a retrieval plan."""
    if not messages:
        return IntentPlan(intent="other", key_terms=[], categories=[],
                          needs_table=False, rationale="empty input")
    history = _render_history(messages[:-1])
    user_question = messages[-1].content

    user_block = (
        f"# Recent conversation\n{history or '(none)'}\n\n"
        f"# Latest question\n{user_question}\n\n"
        "Return JSON per the schema."
    )

    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": planner_system()},
            {"role": "user",   "content": user_block},
        ],
        response_format=_PLANNER_RESPONSE_FORMAT,
        max_completion_tokens=600,
    )
    if meter is not None:
        meter.add("planner", resp.usage)
    content = resp.choices[0].message.content or "{}"
    data = json.loads(content)
    return IntentPlan(**data)
