"""The shipped chat prompts must not carry one deployment's world.

Prompts are engine code that reaches a paid model on every question, so a
domain noun in here steers the model toward the wrong vocabulary for every
adopter, and a real address or a real counterparty name in here ships that
data to everyone who clones the repository.

The seam is per-domain override files (`configs/prompts/chat_*.<doctype>.md`).
A domain that wants sharper prompts writes its own, and the defaults in
`api/rag/prompts.py` stay neutral. These tests hold the defaults to that.
"""
from __future__ import annotations

import re

import pytest

from api.rag import prompts

# Words that would steer the shared defaults toward one narrow deployment. They
# belong in per-domain overrides, not prompts that every adopter inherits.
_DOMAIN_WORDS = (
    "declared load", "engineering contract", "engineering-contract",
    "rth", "kw/rt", "total system efficiency",
)

_DEFAULTS = {
    "planner": prompts._PLANNER_DEFAULT,
    "agent": prompts._AGENT_DEFAULT,
    "synth": prompts._SYNTH_DEFAULT,
}


@pytest.mark.parametrize("name", sorted(_DEFAULTS))
def test_default_prompt_names_no_domain_vocabulary(name):
    text = _DEFAULTS[name].lower()
    found = [w for w in _DOMAIN_WORDS if w in text]
    assert not found, (
        f"the neutral {name} prompt names {found}. Move that guidance into "
        f"configs/prompts/chat_{name}.<doctype>.md and keep the default generic.")


def test_no_street_address_in_the_default_prompts():
    """A worked example that happens to be a real address is still a real
    address once the repository is public."""
    pattern = re.compile(r"\b\d+\s+[A-Z][a-z]+\s+(Way|Street|Road|Avenue|Drive|Lane)\b")
    for name, text in _DEFAULTS.items():
        assert not pattern.search(text), f"{name} prompt contains a street address"


def test_planner_categories_come_from_the_active_domain():
    """The category menu was a 17-value literal from the first domain. A
    planner offered categories its corpus does not have will pick them, and
    cannot pick the ones the corpus does have."""
    from pipeline.extraction import get_analyzer
    from pipeline.ontology import DEFAULT_DOCTYPE
    rendered = prompts.planner_system()
    assert "{categories}" not in rendered, "the category slot was never filled"
    for cat in get_analyzer(DEFAULT_DOCTYPE).categories:
        assert cat in rendered, f"the active domain's category {cat!r} is missing"


def test_an_override_file_replaces_the_default(tmp_path, monkeypatch):
    """Proves the seam works, so a domain can actually customise."""
    monkeypatch.setattr(prompts, "CONFIGS_DIR", tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "chat_agent.demo.md").write_text(
        "custom agent prompt", encoding="utf-8")
    prompts._override.cache_clear()
    try:
        assert prompts._override("chat_agent", "demo") == "custom agent prompt"
        assert prompts._override("chat_agent", "absent") is None
    finally:
        prompts._override.cache_clear()
