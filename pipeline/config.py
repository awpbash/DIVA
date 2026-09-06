from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val or ""


@dataclass(frozen=True)
class Config:
    openai_api_key: str
    # OpenAI-compatible base URL. Empty = api.openai.com (local dev). Set to an
    # Azure AI Foundry OpenAI-compatible endpoint to route all LLM/embedding
    # calls through Foundry (token = openai_api_key, base_url = this). Model
    # names (text_model/reasoning_model/embed_model) must match the Foundry
    # deployment names.
    openai_base_url: str
    # Embeddings may live behind a DIFFERENT endpoint/key than chat (e.g. a
    # separate Foundry embedding deployment). Empty = reuse the chat base_url /
    # api_key above.
    openai_embed_base_url: str
    openai_embed_api_key: str
    vision_model: str
    text_model: str
    # Heavy reasoning model for whole-document understanding (outline,
    # harvest, normalise). These calls fit a 50-page contract into one
    # prompt and need the bigger context window. Defaults to gpt-5.4
    # (1M-context). Keep separate from text_model so chat-API agent /
    # planner / synth calls can stay on a cheaper general-text model.
    reasoning_model: str
    # Model for the per-category normalise pass. PROSE categories use this cheap/
    # fast model (each call is narrow-scoped). Override with OPENAI_NORMALISE_MODEL.
    normalise_model: str
    # VALUE-BEARING categories (rate/charge/measurement, the ones with a `measure`
    # block) instead use this stronger model: they carry multi-value table/schedule
    # rows the cheap model silently under-extracts (drops a column/sibling value).
    # Override with OPENAI_NORMALISE_HEAVY_MODEL.
    normalise_heavy_model: str
    embed_model: str

    # Cosmos DB (NoSQL API). Defaults target the local emulator (vnext,
    # plain HTTP on :8081, well-known dev key) so local dev needs no env
    # vars. Production sets COSMOS_URI + COSMOS_KEY (or managed identity
    # later). ``cosmos_vector_mode``: 'client' = exact in-RAM cosine (dev,
    # emulator-proof), 'native' = VectorDistance + DiskANN on real Azure.
    cosmos_uri: str
    cosmos_key: str
    cosmos_db: str
    cosmos_container: str
    cosmos_vector_mode: str

    # Which reader turns a PDF into pages_md/ blocks. 'rapidocr' = local
    # RapidOCR + LLM correct/classify (free OCR, works offline, the default).
    # 'cu' = Azure Content Understanding (one paid analyzeBinary call per
    # document, layout + tables + figures native, no vision-correction pass).
    reader: str
    # Azure Content Understanding (only used when reader='cu'). Endpoint is
    # the Foundry resource endpoint, e.g. https://<res>.cognitiveservices.azure.com
    cu_endpoint: str
    cu_key: str
    cu_analyzer: str

    storage_root: Path
    render_dpi: int
    vision_concurrency: int

    @classmethod
    def load(cls) -> "Config":
        repo_root = Path(__file__).resolve().parents[1]
        storage = _env("STORAGE_ROOT", "./storage")
        storage_path = (repo_root / storage).resolve() if not os.path.isabs(storage) else Path(storage)
        storage_path.mkdir(parents=True, exist_ok=True)
        return cls(
            # Not required: `Config.load()` runs at import in a dozen
            # modules, so requiring it here would mean an instance with no
            # key can't even import the app, crash-looping with the real
            # reason buried in container logs. The app boots without one,
            # says so, and fails with a clear message only when a model is
            # actually needed. See `make_async_openai` below.
            openai_api_key=_env("OPENAI_API_KEY"),
            openai_base_url=_env("OPENAI_BASE_URL", ""),
            openai_embed_base_url=_env("OPENAI_EMBED_BASE_URL", ""),
            openai_embed_api_key=_env("OPENAI_EMBED_API_KEY", ""),
            vision_model=_env("OPENAI_VISION_MODEL", "gpt-5.6-luna"),
            text_model=_env("OPENAI_TEXT_MODEL", "gpt-5.6-luna"),
            reasoning_model=_env("OPENAI_REASONING_MODEL", "gpt-5.6-terra"),
            normalise_model=_env("OPENAI_NORMALISE_MODEL",
                                 _env("OPENAI_TEXT_MODEL", "gpt-5.6-luna")),
            # Independent of reasoning_model on purpose: a deployment may run
            # reasoning on a cheap model, but the value-category normalise needs
            # a genuinely strong model to split multi-value table rows.
            normalise_heavy_model=_env("OPENAI_NORMALISE_HEAVY_MODEL", "gpt-5.4"),
            embed_model=_env("OPENAI_EMBED_MODEL", "text-embedding-3-large"),
            cosmos_uri=_env("COSMOS_URI", "http://localhost:8081"),
            # The well-known Cosmos emulator key, public by design, dev only.
            cosmos_key=_env("COSMOS_KEY",
                            "C2y6yDjf5/R+ob0N8A7Cgv30VRDJIWEHLM+4QDU5DE2n"
                            "Q9nDuVTqobD4b8mGGyPMbIZnqyMsEcaGQy67XIw/Jw=="),
            cosmos_db=_env("COSMOS_DB", "verbatim"),
            cosmos_container=_env("COSMOS_CONTAINER", "kb"),
            cosmos_vector_mode=_env("COSMOS_VECTOR_MODE", "client"),
            reader=_env("READER", "rapidocr").strip().lower(),
            cu_endpoint=_env("CU_ENDPOINT", "").rstrip("/"),
            cu_key=_env("CU_KEY", ""),
            # prebuilt-layout = words/paragraphs/tables/figures with geometry
            # (verified live on GA 2025-11-01, the docs' older sample name
            # prebuilt-documentAnalyzer does not exist in GA).
            cu_analyzer=_env("CU_ANALYZER", "prebuilt-layout"),
            storage_root=storage_path,
            render_dpi=int(_env("RENDER_DPI", "300")),
            vision_concurrency=int(_env("VISION_PAGE_CONCURRENCY", "4")),
        )


NO_MODEL_KEY = (
    "No model API key is configured, so nothing can be read or answered. "
    "Set OPENAI_API_KEY in .env (any OpenAI-compatible endpoint works, see "
    ".env.example) and restart. `python -m scripts.setup --check` reports it."
)


def _require_model_key(cfg: "Config") -> None:
    if not cfg.openai_api_key:
        raise RuntimeError(NO_MODEL_KEY)


# Passed explicitly rather than left to the SDK's own default. The SDK reads
# OPENAI_BASE_URL from the environment whenever base_url is None, and a
# blank .env line ("OPENAI_BASE_URL=") is present but empty, not absent. The
# SDK then builds every request against an empty host, and every call dies
# with an unhelpful "Request URL is missing an 'http://' or 'https://'
# protocol" error. .env.example ships that line commented out, so
# uncommenting and filling it in later is a normal thing to do.
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"


def make_async_openai(cfg: "Config", **kwargs):
    """The single place an AsyncOpenAI client is built. Routes through an
    OpenAI-compatible base_url (e.g. Azure AI Foundry) when
    ``OPENAI_BASE_URL`` is set, falls back to api.openai.com otherwise.
    Centralised so a future auth-header or api-version tweak lives in one
    spot.

    Raises when no key is configured, with the fix in the message: this is
    the one chokepoint every model call passes through, so an unconfigured
    instance fails here, once, legibly, rather than at import time or as a
    bare 401 from the provider."""
    from openai import AsyncOpenAI
    _require_model_key(cfg)
    return AsyncOpenAI(
        api_key=cfg.openai_api_key,
        base_url=cfg.openai_base_url or OPENAI_DEFAULT_BASE_URL,
        **kwargs,
    )


def make_async_openai_embed(cfg: "Config", **kwargs):
    """AsyncOpenAI client for EMBEDDINGS, which may sit behind a different
    endpoint/key than chat (e.g. a separate Foundry embedding deployment).
    Falls back to the chat base_url/key when the embed-specific vars are unset."""
    from openai import AsyncOpenAI
    if not (cfg.openai_embed_api_key or cfg.openai_api_key):
        _require_model_key(cfg)
    return AsyncOpenAI(
        api_key=cfg.openai_embed_api_key or cfg.openai_api_key,
        base_url=(cfg.openai_embed_base_url or cfg.openai_base_url
                  or OPENAI_DEFAULT_BASE_URL),
        **kwargs,
    )
