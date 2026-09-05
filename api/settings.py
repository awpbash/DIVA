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
    # What this deployment calls the things it holds, since a hardcoded
    # "contract" is wrong for a corpus of medical records or planning
    # applications. Singular and plural, because English.
    document_noun: str = "document"
    document_noun_plural: str = "documents"

    # Vector + graph retrieval. Tight defaults: most chat questions need
    # 1-3 evidences, and bundle padding just dilutes the answer and
    # creates messy citation lists in the UI.
    top_k_evidence: int = 6           # EvidenceSpans pulled by vector recall
    top_k_sections: int = 3           # Sections pulled by vector recall
    fact_neighbors: int = 3           # per fact, how many other facts in same Section to include
    # Floor on retrieval relevance: citations below this score are dropped
    # before reaching synth. Tuned for cosine on text-embedding-3-large,
    # raise if noise creeps back in.
    min_relevance_score: float = 0.55

    # Synth uses full gpt-5.4: mini has been observed collapsing an
    # aggregation caveat into a false zero and dropping the real citation
    # on a money-terms question, and a wrong number here is the worst
    # possible answer. Full does not make this mistake.
    synth_model_override: str | None = "gpt-5.4"
    # Planner: mini is fine here. The prompt rule requiring an explicit
    # request for a definition keeps capitalised role terms like
    # "supplier" from being misread as intent=definition, so mini is not
    # costing accuracy.
    planner_model_override: str | None = "gpt-5.4-mini"
    max_synth_tokens: int = 4000

    # Agent loop: mini is fine here too. It occasionally retries a typed
    # lookup with reworded queries before giving up, a few extra cheap
    # round trips, but lands on the same correct answer as full.
    agent_max_steps: int = 5                  # tool-call rounds before forced stop
    agent_model_override: str | None = "gpt-5.4-mini"

    # Kill-switch for agent tools. Names listed here (comma-separated in
    # the CHAT_DISABLED_TOOLS env var) are stripped from both the schemas
    # the agent sees and from dispatch, so disabling a misbehaving tool is
    # a restart, not a code change.
    disabled_tools: tuple[str, ...] = ()

    # CORS: Vite default plus safe loopback, and in dev any localhost or
    # loopback port so a fallback Vite port doesn't break the preflight.
    # Private-LAN (RFC1918) origins are included for `vite --host` demos
    # from other machines on the office network. Never matches a public
    # internet origin.
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

    # Optional API-key gate. When CHAT_API_KEY is set, every route except
    # /healthz requires the same value in an `X-API-Key` header (or
    # `?api_key=` for direct-link downloads). Unset means auth is off,
    # fine for local dev but an interim measure until real identity is
    # wired in.
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
