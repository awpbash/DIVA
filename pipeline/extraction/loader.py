"""
Loader for the declarative extraction config tree (v2).

Reads ``configs/`` at the repo root, composes per-doctype analyzers, validates
against the JSON meta-schema in ``configs/schemas/analyzer.schema.json``, and
resolves prompt references.

v2 model
--------
Analyzers no longer enumerate fields. They declare:

  * ``categories`` — the fact categories the categorise pass (step 4c) is
    allowed to assign.
  * ``roles`` — a per-category role taxonomy used by the same pass to label
    facts with their domain role (e.g. money → consumption_charge_rate).

Doctype analyzers can ``extends: _universal`` to inherit the universal
category set; their own ``roles`` block is merged on top of the parent's.

Design rules
------------
* **Fail fast.** Any malformed config raises ``ConfigError`` at load time.
* **Pure.** No IO outside the configs tree. No network. No model calls.
* **Idempotent.** ``load_all()`` returns the same composed objects given the
  same files on disk; downstream stages cache by ``ComposedAnalyzer.cache_key``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field as PydField, ValidationError


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS_DIR = _REPO_ROOT / "configs"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised on any malformed config. Always cite the offending file path."""


def _fail(path: Path, msg: str) -> "ConfigError":
    rel = path.relative_to(_REPO_ROOT) if path.is_absolute() else path
    return ConfigError(f"{rel}: {msg}")


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineDefaults:
    """Mirrors configs/pipeline.yaml. Treated as immutable at runtime."""
    render_dpi: int
    image_format: str
    vision_prompt: str
    vision_concurrency: int
    vision_retries: int
    confidence_verified: float
    confidence_tentative: float
    snippet_max_edit_ratio: float
    snippet_case_sensitive: bool


@dataclass(frozen=True)
class ComposedAnalyzer:
    """One fully-composed doctype analyzer, ready for step 4c."""
    id: str
    version: str
    extends: str | None
    classify_when: dict[str, list[str]]
    categories: tuple[str, ...]
    # role lists keyed by category; categories absent here mean "no roles" for that category.
    roles_by_category: dict[str, tuple[str, ...]]
    confidence_verified: float
    confidence_tentative: float
    cache_key: str             # SHA256 over normalized JSON of all inputs

    def roles_for(self, category: str) -> tuple[str, ...]:
        """Roles defined for one category; empty tuple if none."""
        return self.roles_by_category.get(category, ())


# ---------------------------------------------------------------------------
# YAML / JSON helpers
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise _fail(path, "file not found")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise _fail(path, f"YAML parse error: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise _fail(path, f"expected top-level mapping, got {type(data).__name__}")
    return data


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate(meta_schema: dict, instance: dict, path: Path) -> None:
    errors = sorted(
        Draft202012Validator(meta_schema).iter_errors(instance),
        key=lambda e: list(e.absolute_path),
    )
    if not errors:
        return
    msgs = []
    for err in errors:
        loc = ".".join(str(p) for p in err.absolute_path) or "<root>"
        msgs.append(f"  - {loc}: {err.message}")
    raise _fail(path, "schema violation:\n" + "\n".join(msgs))


# ---------------------------------------------------------------------------
# Prompt resolution + Jinja2 rendering
# ---------------------------------------------------------------------------


def _resolve_prompt(name: str) -> Path:
    p = CONFIGS_DIR / "prompts" / f"{name}.md"
    if not p.exists():
        raise ConfigError(f"prompt not found: configs/prompts/{name}.md")
    return p


_JINJA_ENV: Environment | None = None


def _jinja_env() -> Environment:
    global _JINJA_ENV
    if _JINJA_ENV is None:
        # Lazy import to avoid a circular import between loader.py and
        # prompt_helpers.py (helpers don't depend on loader, but isolating
        # the import here keeps module-load order tolerant).
        from .prompt_helpers import render_block, render_grid
        _JINJA_ENV = Environment(
            loader=FileSystemLoader(str(CONFIGS_DIR / "prompts")),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            autoescape=False,
        )
        # Make the block renderer callable from any prompt template.
        _JINJA_ENV.globals["render_block"] = render_block
        _JINJA_ENV.globals["render_grid"] = render_grid
    return _JINJA_ENV


def render_prompt(name: str, **vars: Any) -> str:
    """
    Render ``configs/prompts/<name>.md`` with the given template variables.
    Raises ``ConfigError`` if the file is missing or a referenced variable
    is not supplied (StrictUndefined).
    """
    if not (CONFIGS_DIR / "prompts" / f"{name}.md").exists():
        raise ConfigError(f"prompt not found: configs/prompts/{name}.md")
    try:
        return _jinja_env().get_template(f"{name}.md").render(**vars)
    except TemplateError as e:
        raise ConfigError(f"prompt {name!r} render failed: {e}") from e


# ---------------------------------------------------------------------------
# Pipeline defaults
# ---------------------------------------------------------------------------


class _RenderCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dpi: int = PydField(ge=72, le=600)
    image_format: str = "png"


class _VisionCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str
    concurrency: int = PydField(ge=1, le=64)
    retries: int = PydField(ge=0, le=10)


class _ConfidenceCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verified: float = PydField(ge=0, le=1)
    tentative: float = PydField(ge=0, le=1)


class _LoadCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confidence: _ConfidenceCfg


class _SnippetCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_edit_ratio: float = PydField(ge=0, le=1)
    case_sensitive: bool = False


class _PipelineYaml(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 2
    # The one domain this deployment serves. Read by pipeline.ontology, which
    # sits below this loader and parses the key itself, so it is declared here
    # only to satisfy extra="forbid". Optional: a checkout shipping exactly one
    # pack autodetects it.
    domain: str | None = None
    render: _RenderCfg
    vision: _VisionCfg
    load: _LoadCfg
    snippet: _SnippetCfg


def _load_pipeline_defaults() -> PipelineDefaults:
    path = CONFIGS_DIR / "pipeline.yaml"
    data = _read_yaml(path)
    try:
        cfg = _PipelineYaml.model_validate(data)
    except ValidationError as e:
        msgs = [f"  - {'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
                for err in e.errors()]
        raise _fail(path, "malformed pipeline.yaml:\n" + "\n".join(msgs)) from e

    if cfg.load.confidence.tentative > cfg.load.confidence.verified:
        raise _fail(path, "load.confidence.tentative must be <= verified")

    return PipelineDefaults(
        render_dpi=cfg.render.dpi,
        image_format=cfg.render.image_format,
        vision_prompt=cfg.vision.prompt,
        vision_concurrency=cfg.vision.concurrency,
        vision_retries=cfg.vision.retries,
        confidence_verified=cfg.load.confidence.verified,
        confidence_tentative=cfg.load.confidence.tentative,
        snippet_max_edit_ratio=cfg.snippet.max_edit_ratio,
        snippet_case_sensitive=cfg.snippet.case_sensitive,
    )


# ---------------------------------------------------------------------------
# Analyzer composition
# ---------------------------------------------------------------------------


def _load_one_analyzer(analyzer_dir: Path, meta_schema: dict) -> dict:
    """Read and meta-validate one analyzer.yaml. Returns the raw dict."""
    analyzer_path = analyzer_dir / "analyzer.yaml"
    doc = _read_yaml(analyzer_path)
    _validate(meta_schema, doc, analyzer_path)
    if doc.get("id") != analyzer_dir.name:
        raise _fail(
            analyzer_path,
            f"id={doc.get('id')!r} must match folder name {analyzer_dir.name!r}",
        )
    return doc


def _merge_roles(parent: dict[str, list[str]] | None,
                 child: dict[str, list[str]] | None) -> dict[str, tuple[str, ...]]:
    """
    Merge child roles on top of parent roles, per category.

    Child role lists fully replace parent lists for the same category — we
    don't try to append (a doctype that wants to change one role list
    re-declares it; semantically clearer than partial overrides).
    """
    out: dict[str, tuple[str, ...]] = {}
    for cat, roles in (parent or {}).items():
        out[cat] = tuple(roles)
    for cat, roles in (child or {}).items():
        out[cat] = tuple(roles)
    return out


def _compose_one(
    analyzer_dir: Path,
    parent_doc: dict | None,
    defaults: PipelineDefaults,
    meta_schema: dict,
) -> ComposedAnalyzer:
    own = _load_one_analyzer(analyzer_dir, meta_schema)
    extends = own.get("extends")

    if extends and parent_doc is None:
        raise _fail(
            analyzer_dir / "analyzer.yaml",
            f"extends={extends!r} but parent analyzer not provided",
        )
    if extends and extends != parent_doc.get("id"):
        raise _fail(
            analyzer_dir / "analyzer.yaml",
            f"extends={extends!r} but parent_doc.id={parent_doc.get('id')!r}",
        )

    # Categories: own list wins if set; otherwise inherit parent's.
    categories = tuple(own.get("categories") or (parent_doc or {}).get("categories") or ())
    if not categories:
        raise _fail(analyzer_dir / "analyzer.yaml", "no categories declared (or inherited)")

    # Roles: parent first, child overrides per category.
    roles_by_category = _merge_roles(
        (parent_doc or {}).get("roles") if extends else None,
        own.get("roles"),
    )

    # Validate every role-category appears in the categories list.
    for cat in roles_by_category:
        if cat not in categories:
            raise _fail(
                analyzer_dir / "analyzer.yaml",
                f"roles declared for category {cat!r} but that category is not enabled",
            )

    thresholds = own.get("confidence_thresholds") or {}

    cache_key = _cache_key(
        own_id=own["id"],
        version=str(own["version"]),
        extends=extends,
        categories=categories,
        roles_by_category=roles_by_category,
        defaults=defaults,
    )

    return ComposedAnalyzer(
        id=own["id"],
        version=str(own["version"]),
        extends=extends,
        classify_when=own.get("classify_when") or {},
        categories=categories,
        roles_by_category=roles_by_category,
        confidence_verified=float(thresholds.get("verified", defaults.confidence_verified)),
        confidence_tentative=float(thresholds.get("tentative", defaults.confidence_tentative)),
        cache_key=cache_key,
    )


def _cache_key(*, own_id: str, version: str, extends: str | None,
               categories: tuple[str, ...],
               roles_by_category: dict[str, tuple[str, ...]],
               defaults: PipelineDefaults) -> str:
    payload = {
        "analyzer": {
            "id": own_id,
            "version": version,
            "extends": extends,
            "categories": list(categories),
            "roles_by_category": {k: list(v) for k, v in roles_by_category.items()},
        },
        "defaults": {
            "render_dpi": defaults.render_dpi,
            "vision_prompt": defaults.vision_prompt,
            "confidence_verified": defaults.confidence_verified,
            "confidence_tentative": defaults.confidence_tentative,
            "snippet_max_edit_ratio": defaults.snippet_max_edit_ratio,
        },
    }
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_CACHE: dict[str, ComposedAnalyzer] | None = None
_DEFAULTS_CACHE: PipelineDefaults | None = None


def load_all(refresh: bool = False) -> tuple[PipelineDefaults, dict[str, ComposedAnalyzer]]:
    """Load all configs. Returns (defaults, {analyzer_id: ComposedAnalyzer})."""
    global _CACHE, _DEFAULTS_CACHE
    if not refresh and _CACHE is not None and _DEFAULTS_CACHE is not None:
        return _DEFAULTS_CACHE, _CACHE

    if not CONFIGS_DIR.exists():
        raise ConfigError(f"configs directory not found at {CONFIGS_DIR}")

    meta_schema = _read_json(CONFIGS_DIR / "schemas" / "analyzer.schema.json")
    defaults = _load_pipeline_defaults()

    analyzers_root = CONFIGS_DIR / "analyzers"
    if not analyzers_root.exists():
        raise ConfigError("missing configs/analyzers/")

    # Load _universal first so doctype analyzers can inherit.
    universal_dir = analyzers_root / "_universal"
    universal_doc: dict | None = None
    if universal_dir.exists():
        universal_doc = _load_one_analyzer(universal_dir, meta_schema)

    composed: dict[str, ComposedAnalyzer] = {}

    if universal_doc is not None:
        composed["_universal"] = _compose_one(
            universal_dir,
            parent_doc=None,
            defaults=defaults,
            meta_schema=meta_schema,
        )

    for child in sorted(analyzers_root.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        if not (child / "analyzer.yaml").exists():
            raise ConfigError(f"{child}/: missing analyzer.yaml")
        composed[child.name] = _compose_one(
            child,
            parent_doc=universal_doc,
            defaults=defaults,
            meta_schema=meta_schema,
        )

    _DEFAULTS_CACHE = defaults
    _CACHE = composed
    return defaults, composed


def get_analyzer(analyzer_id: str, refresh: bool = False) -> ComposedAnalyzer:
    _, composed = load_all(refresh=refresh)
    if analyzer_id not in composed:
        raise ConfigError(
            f"analyzer {analyzer_id!r} not found. Available: {sorted(composed)}"
        )
    return composed[analyzer_id]
