"""ask.py — fire one question at the live /chat SSE endpoint and print, compactly,
what the agent did: plan, tool calls per step, the citation bundle, and (unless
--retrieval-only) the synthesized answer. For ops-view answer-vs-gap probing.

Run:
  PYTHONUTF8=1 python -m scripts.ask "who maintains the chillers?" --retrieval-only
  PYTHONUTF8=1 python -m scripts.ask "what is the governing law?"
"""
from __future__ import annotations

import json
import sys

import httpx

from ._session import auth_headers

URL = "http://localhost:8000/chat"


def ask(question: str, *, retrieval_only: bool, role: str | None = None,
        doc_ids: list[str] | None = None) -> None:
    body = {"messages": [{"role": "user", "content": question}],
            "retrieval_only": retrieval_only}
    if role:
        body["role"] = role
    if doc_ids:
        body["doc_ids"] = doc_ids
    print("=" * 100)
    print("Q:", question, "  (retrieval_only)" if retrieval_only else "")
    print("=" * 100)
    answer_parts: list[str] = []
    with httpx.stream("POST", URL, json=body, timeout=180,
                      headers=auth_headers()) as r:
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
                try:
                    payload = json.loads(data)
                except Exception:
                    continue
                if event == "plan":
                    print(f"  PLAN intent={payload.get('intent')!r} "
                          f"terms={payload.get('key_terms')}")
                elif event == "step":
                    tcs = payload.get("tool_calls") or []
                    desc = "; ".join(f"{t['name']}({json.dumps(t.get('args',{}))[:80]})" for t in tcs)
                    print(f"  STEP {payload.get('step')}: {desc or '(no tools)'}")
                elif event == "tool_result":
                    print(f"     ↳ {payload.get('tool')}: n={payload.get('n_results')} "
                          f"new={payload.get('n_new')} count={payload.get('count')} "
                          f"{('ERR '+str(payload.get('error'))) if payload.get('error') else ''}")
                elif event == "citations":
                    cits = payload.get("citations") or []
                    print(f"  CITATIONS ({len(cits)}):")
                    for c in cits[:8]:
                        snip = (c.get("snippet") or "").replace("\n", " ")[:90]
                        print(f"     [{c.get('confidence_tier')}/{c.get('citation_kind')}] "
                              f"{c.get('doc_id')} score={round(c.get('score',0),2)}  {snip!r}")
                elif event == "token":
                    answer_parts.append(payload.get("delta", ""))
                elif event == "error":
                    print("  ERROR:", payload.get("message"))
    if answer_parts:
        print("  ANSWER:", "".join(answer_parts).strip()[:700])
    print()


if __name__ == "__main__":
    argv = sys.argv[1:]
    ro = "--retrieval-only" in argv
    role = None
    if "--role" in argv:
        i = argv.index("--role")
        role = argv[i + 1]
        del argv[i:i + 2]
    doc_ids = None
    if "--doc" in argv:
        i = argv.index("--doc")
        doc_ids = [argv[i + 1]]
        del argv[i:i + 2]
    args = [a for a in argv if a != "--retrieval-only"]
    ask(" ".join(args), retrieval_only=ro, role=role, doc_ids=doc_ids)
