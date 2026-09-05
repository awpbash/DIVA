"""OpenAI tool-call schemas handed to the agent loop.

Names match the keys in ``TOOL_DISPATCH``. The agent loop hands these to
``openai.chat.completions.create(tools=...)`` and dispatches the resulting
tool_calls by name.
"""
from __future__ import annotations

from pipeline.ontology import DEFAULT_DOCTYPE

from ._shared import _id_key_by_label

# Skipped when no domain is configured yet (DEFAULT_DOCTYPE == "", see
# pipeline/ontology.py): _id_key_by_label() calls load_pack() with no
# doctype, which would try to open configs/packs/.yaml and crash this
# module's import before the setup wizard ever gets a chance to run. A
# restart always follows the wizard finishing (see api/routes/setup.py),
# so this module gets re-imported with a real domain once one exists —
# nothing here is ever actually served against the empty sentinel.
_LABEL_ENUM = sorted(_id_key_by_label()) if DEFAULT_DOCTYPE else []


OPENAI_TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "vector_search_evidence",
            "description": (
                "Fuzzy semantic recall over EvidenceSpans. Use when no direct "
                "label/section lookup fits — open-ended questions, paraphrased "
                "concepts, or 'find anything about X'. Also returns "
                "low-confidence raw FactMention fragments when query terms "
                "overlap dropped fine-grained spans."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "The text to embed and search. "
                                             "Use the user's wording + key entities."},
                    "k":     {"type": "integer", "minimum": 1, "maximum": 30,
                              "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vector_search_sections",
            "description": (
                "Section-level semantic recall. Use for vague navigational "
                "questions ('what does Schedule 1B cover?', 'is there a "
                "clause about X?') where the answer is a whole section."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k":     {"type": "integer", "minimum": 1, "maximum": 10,
                              "default": 4},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vector_search_blocks",
            "description": (
                "Block-level semantic recall over raw paragraph / figure / "
                "table text. CALL THIS FIRST for any question about a diagram, "
                "schematic, figure, floor plan, or chart — the answer is RAW "
                "BLOCK TEXT inside the figure and is NOT in any typed fact, "
                "so `vector_search_evidence` will miss it. Examples: 'what "
                "does the electrical schematic show?', 'what's in Figure 1A?', "
                "'describe the floor plan', 'what equipment appears in the "
                "diagram?'. Also useful as a fallback when other vector "
                "searches return weak hits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k":     {"type": "integer", "minimum": 1, "maximum": 10,
                              "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "keyword_search",
            "description": (
                "Lexical recall over evidence text AND raw paragraph "
                "text — the exact-token partner to vector_search_evidence. "
                "Reach for it when the LITERAL CHARACTERS matter more than "
                "meaning: a model or spec string, a part number, a rating, "
                "an acronym, or a phrase the user quoted verbatim. Because it "
                "also searches raw "
                "paragraphs, it can find wording NO extracted field ever "
                "captured — the safety net when structured lookups return "
                "nothing. Often worth calling ALONGSIDE vector search — the "
                "bundle fuses both."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "The exact terms / phrase to match."},
                    "k":     {"type": "integer", "minimum": 1, "maximum": 30,
                              "default": 8},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_section",
            "description": (
                "Exact / prefix lookup for a clause number ('9.3', '9', "
                "'7.1(b)'). Use when the user names a section. Prefix match: "
                "'9' returns 9, 9.1, 9.2, ... — pass the most specific number "
                "the user gave."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section_num": {"type": "string",
                                    "description": "e.g. '9.3' or '7.1(b)'"},
                    "k":           {"type": "integer", "minimum": 1, "maximum": 50,
                                    "default": 20},
                },
                "required": ["section_num"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_defined_term",
            "description": (
                "Lookup a capitalised defined term, the kind a document "
                "defines once and then uses throughout. Returns the "
                "document's own definition. Use "
                "for intent=definition or whenever the question hinges on a "
                "specific defined term's meaning. When the graph has "
                "CanonicalTerm links, this can expand to same-named terms "
                "across documents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {"type": "string",
                             "description": "The term as the user wrote it; "
                                            "casing is normalised internally."},
                    "k":    {"type": "integer", "minimum": 1, "maximum": 10,
                             "default": 5},
                },
                "required": ["term"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_verified_fields",
            "description": (
                "The ALIGNED, human-reviewed knowledge base — one named field "
                "per document (schema-first extraction + review). CALL THIS "
                "FIRST for any question about a NAMED field: amounts, rates "
                "and fees, party names, dates, quantities, responsibilities, "
                "or how one document relates to another. Values are "
                "current-only (superseded statements excluded) and each result "
                "is tagged HUMAN-VERIFIED or AI-extracted — repeat that tag in "
                "your answer so the reader knows the trust level. Search with "
                "the user's own topic words. Falls back cleanly: if this "
                "returns nothing, fall back to vector search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Field name, topic, or the user's own words."},
                    "include_superseded": {
                        "type": "boolean", "default": False,
                        "description": "Also return older superseded statements (history questions)."},
                    "k": {"type": "integer", "minimum": 1, "maximum": 30, "default": 12},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aggregate_ops_fields",
            "description": (
                "TRUE numeric aggregation over the ALIGNED knowledge base "
                "(the human-reviewed ontology fields) — sum/avg/min/max/count "
                "a field's numeric values across CURRENT statements, corpus-"
                "wide or per contract family. Use this whenever "
                "the quantity is a named ontology field (a total, an average or "
                "an extreme of one field across documents) — values "
                "are per-field-supersedence current and trust-tagged. `field` "
                "takes plain words; it resolves to the best ontology field. "
                "op='list' returns the per-document values without arithmetic."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    # No example field name here on purpose: the schema is
                    # per-deployment, so any name written in would be fiction on
                    # every domain but the one it came from. The resolver takes
                    # the words as the user said them.
                    "field": {"type": "string",
                              "description": "The field in plain words, or its full "
                                             "'category.field_key'."},
                    "op":    {"type": "string",
                              "enum": ["sum", "avg", "min", "max", "count", "list"],
                              "default": "sum"},
                    "group": {"type": "string",
                              "description": "Optional contract-family (Document.group) "
                                             "to scope to one family."},
                },
                "required": ["field"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "expand_cross_refs",
            "description": (
                "Follow REFERENCES_SECTION edges from sections already in "
                "your evidence bundle. Use when the user asks 'what does "
                "clause X refer to?' or when a clause you found mentions "
                "another section you haven't looked at yet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "section_id values from prior tool "
                                       "results. The tool walks "
                                       "REFERENCES_SECTION edges out of these.",
                    },
                    "k": {"type": "integer", "minimum": 1, "maximum": 30,
                          "default": 12},
                },
                "required": ["section_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_orphan_fragments",
            "description": (
                "LAST-RESORT recall over raw harvested spans that were "
                "dropped during fact extraction (orphans — they back no "
                "typed fact, so no other tool can see them). Use ONLY when "
                "something clearly exists in the document but every typed "
                "lookup and vector search returned nothing — e.g. an obscure "
                "measurement or stray number, OR a whole provision the "
                "extractor could not categorise (governing law, dispute "
                "resolution / arbitration, a liability cap, an insurance or "
                "deliverables list, the document title). Lower confidence: the "
                "spans were dropped for a reason, so read the snippet before "
                "trusting it. Pass the user's key terms as the query."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Key terms to keyword-match "
                                             "against the raw spans."},
                    "raw_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional harvest-label filter, e.g. "
                                       "['measurement'] or ['money','rate']. "
                                       "Omit to search all non-boilerplate "
                                       "labels.",
                    },
                    "k": {"type": "integer", "minimum": 1, "maximum": 20,
                          "default": 6},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_document_assets",
            "description": (
                "Find full-page DIAGRAMS, SCHEMATICS, DRAWINGS or figure "
                "pages inside the documents: site plans, system drawings, "
                "flow sheets, layouts, detail views. These pages are images "
                "with "
                "almost no text, so NO other tool can find them. Use whenever "
                "the user asks WHERE a diagram/schematic/drawing/layout is, "
                "or asks to see or be directed to one. Returns the document + "
                "page as a citation — cite it so the user can open the page. "
                "The tool points at the diagram; it cannot read its contents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "What the user is looking for, "
                                             "in their own words."},
                    "k": {"type": "integer", "minimum": 1, "maximum": 20,
                          "default": 6},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Signal you have enough evidence to answer. Call this when "
                "the citation bundle covers the question, or when further "
                "tool calls won't add useful evidence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rationale": {"type": "string",
                                  "description": "One sentence: why you're "
                                                 "stopping (for debugging)."},
                },
                "required": ["rationale"],
            },
        },
    },
]
