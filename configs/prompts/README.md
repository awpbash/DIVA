# configs/prompts

Every prompt the system sends to a model, as a file rather than a string in
Python. Two reasons: a prompt is content, not code, and a domain can replace
one without touching the engine.

## Resolution

A prompt resolves in two steps:

```
<name>.<domain>.md    this domain's own version, if it exists
<name>.md             the generic fallback
```

An override REPLACES the default wholesale. That is deliberate rather than a
limitation. Filling your domain's nouns into one shared prompt produces a prompt
that performs worse in every domain, so the honest unit of customisation is the
whole file. Nothing is required: a domain that writes no overrides uses the
generic versions and works.

## What ships

| File | Used by | What it does |
| --- | --- | --- |
| `correct_and_classify.md` | `pipeline/extraction/correct_classify.py` | One vision call per page: correct the OCR against the page image and classify each block as heading, paragraph or table. |
| `table_repair.md` | `pipeline/extraction/table_repair.py` | Rebuild the row and column structure of one table block that flat OCR flattened into text. |
| `field_extract.md` | `pipeline/kb/field_llm.py` | The extraction persona. Reads the whole document and fills the ops-view fields, each anchored to the block marker it came from. This is the one most worth overriding. |

Three more names are recognised and have no generic default, because the
engine's own versions live in `api/rag/prompts.py`:

| Name | Used by | Override to change |
| --- | --- | --- |
| `chat_planner.<domain>.md` | `api/rag/planner.py` | How a question is classified into an intent and key terms. |
| `chat_agent.<domain>.md` | `api/rag/agent.py` | Tool routing: which retrieval tool answers which shape of question. |
| `chat_synth.<domain>.md` | `api/rag/synth.py` | How the final answer is written, cited and abstained from. |

The chat prompts are read raw, not rendered, because they carry literal braces.
The extraction prompts above them are Jinja templates. If you override one of
those, the variables it is rendered with are the ones the calling module passes,
so read that module before changing the shape.

## Writing one

Copy the generic file, keep its structure, and change the parts that are about
your documents rather than about the system. The structural instructions earn
their place: citation discipline, abstention, the rule that a value must be
backed by the block it cites. Those are what make the answers trustworthy and
they are not domain-specific. The vocabulary, the worked examples and the units
are yours.
