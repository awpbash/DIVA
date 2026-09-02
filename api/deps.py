"""Shared singletons.

Both the Cosmos store and the OpenAI client are connection-pool wrappers —
expensive to construct, cheap to share. The app's lifespan (see main.py)
opens them at startup and closes at shutdown; route handlers grab them
through these getters.
"""
from __future__ import annotations

import logging
from typing import Optional

from openai import AsyncOpenAI

from pipeline.config import NO_MODEL_KEY, make_async_openai, make_async_openai_embed
from pipeline.store.aio import AsyncCosmosStore
from .settings import get_config


# Module-level so lifespan can swap them in/out without forcing every route
# to pass a Request object.
_store: Optional[AsyncCosmosStore] = None
_openai_client: Optional[AsyncOpenAI] = None
# Embeddings may live behind a different endpoint than chat — a separate client.
_openai_embed_client: Optional[AsyncOpenAI] = None


def get_store() -> AsyncCosmosStore:
    if _store is None:
        raise RuntimeError("Cosmos store not initialised — call init() in lifespan.")
    return _store


def get_openai() -> AsyncOpenAI:
    if _openai_client is None:
        raise RuntimeError(NO_MODEL_KEY if not get_config().openai_api_key
                           else "OpenAI client not initialised — call init() in lifespan.")
    return _openai_client


def get_openai_embed() -> AsyncOpenAI:
    """Client for EMBEDDINGS (may target a different base_url than chat)."""
    if _openai_embed_client is None:
        raise RuntimeError(NO_MODEL_KEY if not get_config().openai_api_key
                           else "OpenAI embed client not initialised — call init().")
    return _openai_embed_client


async def init() -> None:
    """Open the long-lived connections. Idempotent."""
    global _store, _openai_client, _openai_embed_client
    cfg = get_config()
    # The Azure SDK logs every request at INFO — mute to warnings so app
    # logs stay readable (structured logging owns the signal).
    logging.getLogger("azure").setLevel(logging.WARNING)
    if _store is None:
        _store = AsyncCosmosStore(cfg)
    # No key: leave the clients unbuilt and let the app serve anyway. Sign-in,
    # the documents already read, and every free surface still work, and the
    # getters above explain the one thing that does not. An instance that
    # refuses to start teaches an operator nothing.
    if cfg.openai_api_key:
        if _openai_client is None:
            # 60s per-request timeout so a stalled OpenAI call surfaces as an
            # error instead of hanging the SSE stream forever. Routes through a
            # compatible endpoint when OPENAI_BASE_URL is set.
            _openai_client = make_async_openai(cfg, timeout=60.0)
        if _openai_embed_client is None:
            # Separate client — embeddings may use a different base_url/key.
            _openai_embed_client = make_async_openai_embed(cfg, timeout=60.0)
    else:
        logging.getLogger("api").warning(
            "no model API key configured: reading documents and answering "
            "questions will fail until OPENAI_API_KEY is set")


async def shutdown() -> None:
    global _store, _openai_client, _openai_embed_client
    if _store is not None:
        await _store.close()
        _store = None
    if _openai_client is not None:
        await _openai_client.close()
        _openai_client = None
    if _openai_embed_client is not None:
        await _openai_embed_client.close()
        _openai_embed_client = None
