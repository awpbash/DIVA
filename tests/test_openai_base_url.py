"""A blank OPENAI_BASE_URL must not disable the default endpoint.

The OpenAI SDK reads OPENAI_BASE_URL from the environment whenever base_url is
None, and it treats present-but-empty as set. A .env carrying the line with
nothing after the equals sign therefore pointed every request at an empty host,
and the only symptom was "Request URL is missing an 'http://' or 'https://'
protocol" from deep inside httpx. .env.example ships that line commented out,
so writing it before you have a value for it is an ordinary thing to do.
"""
from __future__ import annotations

import pytest

from pipeline.config import (
    OPENAI_DEFAULT_BASE_URL, Config, make_async_openai, make_async_openai_embed,
)


def _cfg(**over) -> Config:
    base = {"openai_api_key": "sk-test", "openai_base_url": "",
            "openai_embed_base_url": "", "openai_embed_api_key": ""}
    base.update(over)
    return Config(**{f.name: base.get(f.name, getattr(Config, f.name, None))
                     for f in Config.__dataclass_fields__.values()})


@pytest.fixture(autouse=True)
def _blank_env(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    monkeypatch.setenv("OPENAI_EMBED_BASE_URL", "")


def test_chat_client_falls_back_to_the_real_endpoint():
    c = make_async_openai(_cfg())
    assert str(c.base_url).rstrip("/") == OPENAI_DEFAULT_BASE_URL


def test_embed_client_falls_back_to_the_real_endpoint():
    c = make_async_openai_embed(_cfg())
    assert str(c.base_url).rstrip("/") == OPENAI_DEFAULT_BASE_URL


def test_a_configured_endpoint_still_wins():
    c = make_async_openai(_cfg(openai_base_url="http://gateway.internal/v1"))
    assert str(c.base_url).rstrip("/") == "http://gateway.internal/v1"


def test_the_embed_endpoint_overrides_the_chat_one():
    c = make_async_openai_embed(_cfg(openai_base_url="http://chat.internal/v1",
                                     openai_embed_base_url="http://emb.internal/v1"))
    assert str(c.base_url).rstrip("/") == "http://emb.internal/v1"


def test_embeddings_inherit_the_chat_endpoint_when_unset():
    c = make_async_openai_embed(_cfg(openai_base_url="http://chat.internal/v1"))
    assert str(c.base_url).rstrip("/") == "http://chat.internal/v1"
