"""ontology.py — loader for the declared graph schema.

The ontology YAML (configs/ontology/<doctype>.yaml) is THE single source
of truth for node labels, key properties, and relationship types.
tests/extraction/test_ontology.py enforces that the loader code and the
declaration never drift apart; query-side code can use the helpers here
instead of hardcoding label lists.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"
ONTOLOGY_DIR = _CONFIGS_DIR / "ontology"
_PACKS_DIR = _CONFIGS_DIR / "packs"


def available_doctypes() -> list[str]:
    """Every domain this checkout ships a pack for.

    Names starting with `_` are shared fragments (`_base`), not domains.
    """
    if not _PACKS_DIR.is_dir():
        return []
    return sorted(p.stem for p in _PACKS_DIR.glob("*.yaml")
                  if not p.stem.startswith("_"))


def resolve_active_doctype(*, required: bool = False) -> str:
    """The single domain this deployment serves.

    One instance serves one domain, so the domain is resolved once here rather
    than threaded through every call. Resolution order:

      1. ``VERBATIM_DOMAIN`` in the environment, so a deployment can switch
         domain without editing a file.
      2. ``domain:`` in configs/pipeline.yaml, the declared default.
      3. Autodetect, when the checkout ships exactly one pack. This is the
         normal case for an adopter who wrote one domain and never thought
         about this setting at all.

    Guessing is wrong for the ambiguous case (zero packs, or more than one
    with nothing declared): it would silently extract every document against
    the wrong schema. ``required=True`` raises ``RuntimeError`` there, for a
    caller with a human watching (``scripts/setup.py``) where a clear failure
    beats a silent one. The default, ``required=False``, returns ``""``
    instead: this function also runs at import time (see ``DEFAULT_DOCTYPE``
    below), and a process that cannot finish importing can never reach the
    setup wizard that would let someone actually fix the problem. Every route
    except the wizard is gated shut until a real domain is configured (see
    ``api/main.py``), so nothing downstream ever does real work against the
    empty sentinel.
    """
    env = os.environ.get("VERBATIM_DOMAIN", "").strip()
    if env:
        return env
    path = _CONFIGS_DIR / "pipeline.yaml"
    if path.exists():
        declared = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("domain")
        if declared:
            return str(declared).strip()
    found = available_doctypes()
    if len(found) == 1:
        return found[0]
    if required:
        raise RuntimeError(
            "No active domain. Set VERBATIM_DOMAIN in the environment, or add "
            "`domain: <name>` to configs/pipeline.yaml. Packs found: "
            f"{found or 'none'}."
        )
    return ""


# Resolved once at import. Every module that needs the active domain imports
# this rather than naming a domain, which is what keeps the engine free of the
# example domain's name. "" means no domain is configured yet (fresh install,
# setup wizard not yet run) — see the docstring above.
DEFAULT_DOCTYPE = resolve_active_doctype()


@lru_cache(maxsize=None)
def load_ontology(doctype: str = DEFAULT_DOCTYPE) -> dict[str, Any]:
    path = ONTOLOGY_DIR / f"{doctype}.yaml"
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def node_labels(ont: dict[str, Any]) -> set[str]:
    return set(ont["node_types"])


def fact_labels(ont: dict[str, Any]) -> set[str]:
    return set(ont["layers"]["fact"])


def relationship_names(ont: dict[str, Any]) -> set[str]:
    return set(ont["relationships"])


def key_property(ont: dict[str, Any], label: str) -> str:
    return ont["node_types"][label]["key"]


def relationship_endpoints(ont: dict[str, Any], rel: str) -> tuple[set[str], set[str]]:
    spec = ont["relationships"][rel]
    return set(spec["from"]), set(spec["to"])


def relationship_endpoint_pairs(ont: dict[str, Any], rel: str) -> set[tuple[str, str]]:
    """Exact label pairs for a relationship.

    Most relationships use the Cartesian product of `from` and `to`. Some
    polymorphic relationship types need tighter semantics, such as
    Party->CanonicalParty and DefinedTerm->CanonicalTerm under RESOLVES_TO.
    Those relationships declare `pairs` in the ontology.
    """
    spec = ont["relationships"][rel]
    pairs = spec.get("pairs")
    if pairs:
        return {(str(src), str(dst)) for src, dst in pairs}
    return {(src, dst) for src in spec["from"] for dst in spec["to"]}


# ---------------------------------------------------------------------------
# Extraction contract (R3-D1) — the `extraction:` / `identity:` sections.
# Consumed via pipeline/extraction/ontology_compile.py, which turns these
# into the objects the pipeline stages import.
# ---------------------------------------------------------------------------


def extraction_categories(ont: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return dict(ont.get("extraction", {}).get("categories", {}))


def known_categories(ont: dict[str, Any]) -> frozenset[str]:
    """Every category extraction may legitimately produce — including
    edge-only ones (label: null). Facts outside this set get quarantined."""
    return frozenset(extraction_categories(ont))


def category_label_map(ont: dict[str, Any]) -> dict[str, str]:
    """category -> graph node label, ONLY for categories that load as nodes.
    Edge-only categories (label: null, e.g. reference) are absent — matching
    the historical CATEGORY_TO_LABEL contract."""
    return {
        cat: spec["label"]
        for cat, spec in extraction_categories(ont).items()
        if spec.get("label")
    }


def raw_label_map(ont: dict[str, Any]) -> dict[str, str | None]:
    """harvest raw_label -> category. Dropped labels map to None."""
    out: dict[str, str | None] = {}
    for cat, spec in extraction_categories(ont).items():
        for rl in spec.get("raw_labels") or []:
            out[rl] = cat
    for rl in ont.get("extraction", {}).get("dropped_raw_labels") or []:
        out[rl] = None
    return out


def identity_policy(ont: dict[str, Any]) -> dict[str, Any]:
    return dict(ont.get("identity", {}))


def assertable_relations(ont: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The closed relation menu the R3-D3 relate pass may offer the LLM."""
    return {
        name: spec
        for name, spec in ont["relationships"].items()
        if spec.get("assertable")
    }


def relation_proposal_specs(ont: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """LLM proposal menu.

    These are candidate semantic relations written as Proposal nodes for
    review, not relationship types the loader asserts directly.
    """
    return dict(ont.get("relation_proposals", {}).get("relations", {}))
