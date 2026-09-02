"""The active-domain seam: one deployment serves one domain.

Before this existed, several call sites named the domain as a string literal,
including ingestion and KB rebuild entry points, which meant another domain
could not be ingested or rebuilt whatever config a user wrote.

Two things are locked here: how the active domain resolves, and that no module
in the engine names a domain in code again.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pipeline import ontology

REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------

def _isolate(monkeypatch, tmp_path, packs: list[str], yaml_text: str | None):
    """Point the resolver at a throwaway configs dir."""
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir()
    for name in packs:
        (packs_dir / f"{name}.yaml").write_text("{}", encoding="utf-8")
    # `_base` is a shared fragment every pack extends, never a domain.
    (packs_dir / "_base.yaml").write_text("{}", encoding="utf-8")
    if yaml_text is not None:
        (tmp_path / "pipeline.yaml").write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(ontology, "_CONFIGS_DIR", tmp_path)
    monkeypatch.setattr(ontology, "_PACKS_DIR", packs_dir)


def test_env_var_wins_over_everything(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, ["alpha", "beta"], "domain: alpha\n")
    monkeypatch.setenv("VERBATIM_DOMAIN", "beta")
    assert ontology.resolve_active_doctype() == "beta"


def test_config_key_used_when_env_absent(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, ["alpha", "beta"], "domain: alpha\n")
    monkeypatch.delenv("VERBATIM_DOMAIN", raising=False)
    assert ontology.resolve_active_doctype() == "alpha"


def test_single_pack_autodetects_with_no_config_at_all(monkeypatch, tmp_path):
    """The normal case for an adopter who wrote one domain and never touched
    this setting."""
    _isolate(monkeypatch, tmp_path, ["only_domain"], None)
    monkeypatch.delenv("VERBATIM_DOMAIN", raising=False)
    assert ontology.resolve_active_doctype() == "only_domain"


def test_ambiguous_checkout_raises_when_required(monkeypatch, tmp_path):
    """Guessing between two domains would silently extract every document
    against the wrong schema, so a caller with a human watching (e.g.
    scripts/setup.py) must get a loud failure."""
    _isolate(monkeypatch, tmp_path, ["alpha", "beta"], None)
    monkeypatch.delenv("VERBATIM_DOMAIN", raising=False)
    with pytest.raises(RuntimeError, match="No active domain"):
        ontology.resolve_active_doctype(required=True)


def test_ambiguous_checkout_returns_sentinel_by_default(monkeypatch, tmp_path):
    """At import time there is no human to show a traceback to, and a crash
    here would take the whole process down before the setup wizard could ever
    run. The default call returns the empty-string sentinel instead."""
    _isolate(monkeypatch, tmp_path, ["alpha", "beta"], None)
    monkeypatch.delenv("VERBATIM_DOMAIN", raising=False)
    assert ontology.resolve_active_doctype() == ""


def test_no_packs_returns_sentinel_by_default(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path, [], None)
    monkeypatch.delenv("VERBATIM_DOMAIN", raising=False)
    assert ontology.resolve_active_doctype() == ""


def test_base_fragment_is_not_a_domain(monkeypatch, tmp_path):
    """`_base.yaml` sits in the packs dir but is a shared fragment. If it were
    counted, the single-pack autodetect would never fire."""
    _isolate(monkeypatch, tmp_path, ["only_domain"], None)
    assert ontology.available_doctypes() == ["only_domain"]


def test_shipped_checkout_resolves(monkeypatch):
    """The real repo resolves to a real pack, whatever it is set to."""
    monkeypatch.delenv("VERBATIM_DOMAIN", raising=False)
    assert ontology.resolve_active_doctype() in ontology.available_doctypes()


# ---------------------------------------------------------------------------
# No module names a domain in code
# ---------------------------------------------------------------------------

# Deliberate, documented exceptions. Each is a name that outlived its meaning
# and cannot be changed without breaking something live.
#
# This set is EMPTY, and keeping it that way is the point. Three entries used to
# sit here excusing a customer's name in shipped engine code: the on-disk cache
# prefix, a legacy overlay filename, and a retrieval stopword. Each was a
# one-machine migration shim, and together they meant the export's own content
# scanner had to be told to ignore the exact word it exists to catch. All three
# are gone. Add an entry only for a name that genuinely cannot change, and write
# down what breaks if it does.
_ALLOWED: set[str] = set()

# Files the rule does not apply to. The rule is about ENGINE code selecting its
# domain through DEFAULT_DOCTYPE. The exporter is a maintainer tool that never
# ships (it is in its own PRUNE_NAMES), and knowing which domain does NOT ship
# is precisely its job, so naming one there is the correct implementation
# rather than a leak.
_EXEMPT_FILES = {Path("scripts") / "export_oss.py"}

_TREES = ("pipeline", "api", "scripts")


def _domain_names() -> set[str]:
    return set(ontology.available_doctypes())


def _string_literals(path: Path) -> list[str]:
    """Every string literal in a file EXCEPT docstrings.

    Comments never reach the AST, and docstrings are excluded deliberately:
    naming a domain while explaining something is documentation, not coupling.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = node.body[0] if node.body else None
            if (isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant)
                    and isinstance(doc.value.value, str)):
                docstrings.add(id(doc.value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


def test_no_engine_module_names_a_domain_in_code():
    """The engine selects its domain through `DEFAULT_DOCTYPE`, never by
    spelling one out. This is the guard that keeps the example domain's name
    from creeping back into code."""
    domains = _domain_names()
    offenders: list[str] = []
    for tree in _TREES:
        for path in (REPO / tree).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if path.relative_to(REPO) in _EXEMPT_FILES:
                continue
            for lit in _string_literals(path):
                if lit in _ALLOWED:
                    continue
                if lit in domains:
                    offenders.append(f"{path.relative_to(REPO)}: {lit!r}")
    assert not offenders, (
        "these modules name a domain in code instead of using DEFAULT_DOCTYPE:\n  "
        + "\n  ".join(offenders))


def test_the_guard_would_actually_catch_a_regression(tmp_path):
    """A drift gate nobody has seen fail is not known to work."""
    bad = tmp_path / "bad.py"
    domain = sorted(_domain_names())[0]
    bad.write_text(f'pack = load_pack("{domain}")\n', encoding="utf-8")
    assert domain in _string_literals(bad)


def test_docstrings_are_not_treated_as_coupling(tmp_path):
    ok = tmp_path / "ok.py"
    domain = sorted(_domain_names())[0]
    ok.write_text(f'"""Explains {domain} as an example."""\n', encoding="utf-8")
    assert _string_literals(ok) == []
