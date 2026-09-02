"""Frontend-tunable settings.

Wraps ``pipeline.config.Config`` and adds chat-only knobs that don't belong
on the KB-build config. Single source of truth for retrieval/synth tuning.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from pipeline.config import Config


@dataclass(frozen=True)
class Settings:
    # Branding. One place, read at runtime and served to the browser by
    # GET /branding, so renaming an instance is an env var and a restart
    # rather than a frontend rebuild.
    app_name: str = "Verbatim"
    app_tagline: str = "Every answer traced to the clause it came from"
    # What this deployment CALLS the things it holds. Every screen said
    # "contract", which is wrong for a corpus of medical records or planning
    # applications and reads as somebody else's product. Singular and plural,
    # because English.
    document_noun: str = "document"
    document_noun_plural: str = "documents"

    # Vector + graph retrieval. Tight defaults — most chat questions
    # need 1-3 evidences; bundle padding just dilutes the answer and
    # creates messy citation lists in the UI.
    top_k_evidence: int = 6           # EvidenceSpans pulled by vector recall
    top_k_sections: int = 3           # Sections pulled by vector recall
    fact_neighbors: int = 3           # per fact, how many other facts in same Section to include
    # Floor on retrieval relevance — citations whose score falls below
    # this get dropped before reaching synth. Tuned for cosine on
    # text-embedding-3-large; raise if you see noise creeping back in.
    min_relevance_score: float = 0.55

    # Synth — gpt-5.4 (full) follows the synth prompt's citation
    # discipline + TOTALS preamble much more reliably than mini. The
    # eval showed mini dropping citation segments and undercounting
    # aggregations even with explicit prompt guards.
    synth_model_override: str | None = "gpt-5.4-mini"
    # Planner with mini was misclassifying "who is the supplier?" as
    # `intent=definition` (because "Supplier" is a capitalised contract
    # term), which then biased the agent toward defined_term lookups.
    # Full gpt-5.4 reads the rule about "explicitly asks for a definition".
    planner_model_override: str | None = "gpt-5.4-mini"
    max_synth_tokens: int = 4000

    # Agent loop — gpt-5.4 for tool-routing accuracy. mini was choosing
    # vector_search over typed lookups even when the prompt explicitly
    # routed "who is the supplier" → Party.role.
    agent_max_steps: int = 5                  # tool-call rounds before forced stop
    agent_model_override: str | None = "gpt-5.4-mini"

    # Kill-switch for agent tools. Names listed here (comma-separated in the
    # CHAT_DISABLED_TOOLS env var) are stripped from the schemas the agent
    # sees AND from dispatch — disabling a misbehaving tool is a restart,
    # not a code change. Rollback lever for every new retrieval tool.
    disabled_tools: tuple[str, ...] = ()

    # CORS — Vite default + safe loopback. In dev we also accept any
    # localhost/loopback port via cors_origin_regex so a fallback Vite
    # port (5174, 5175, ...) doesn't break the chat preflight.
    # Private-LAN (RFC1918) origins are included so a `vite --host` demo
    # works from other machines on the office network; this never matches
    # a public internet origin.
    cors_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
    )
    cors_origin_regex: str = (
        r"http://(localhost|127\.0\.0\.1|\[::1\]"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(:\d+)?"
    )

    # Optional API-key gate. When CHAT_API_KEY is set in the environment,
    # every route except /healthz requires the same value in an
    # `X-API-Key` header (or `?api_key=` for direct-link downloads).
    # Unset = auth off (local dev). Interim measure until a real identity
    # provider is wired in.
    api_key: str | None = None


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config.load()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    raw = os.environ.get("CHAT_DISABLED_TOOLS", "")
    disabled = tuple(t.strip() for t in raw.split(",") if t.strip())
    kwargs: dict = {"disabled_tools": disabled}
    # Deployment-specific overrides, all optional:
    #   CHAT_CORS_ORIGINS       comma-separated exact origins
    #   CHAT_CORS_ORIGIN_REGEX  replaces the dev LAN regex entirely
    #   CHAT_API_KEY            enables the API-key gate
    origins_raw = os.environ.get("CHAT_CORS_ORIGINS", "")
    if origins_raw.strip():
        kwargs["cors_origins"] = tuple(
            o.strip() for o in origins_raw.split(",") if o.strip())
    regex_raw = os.environ.get("CHAT_CORS_ORIGIN_REGEX")
    if regex_raw:
        kwargs["cors_origin_regex"] = regex_raw
    api_key = os.environ.get("CHAT_API_KEY")
    if api_key:
        kwargs["api_key"] = api_key
    #   APP_NAME / APP_TAGLINE   what this instance calls itself
    for env_name, field in (("APP_NAME", "app_name"),
                            ("APP_TAGLINE", "app_tagline"),
                            ("APP_DOCUMENT_NOUN", "document_noun"),
                            ("APP_DOCUMENT_NOUN_PLURAL", "document_noun_plural")):
        val = os.environ.get(env_name, "").strip()
        if val:
            kwargs[field] = val
    return Settings(**kwargs)
