"""api/routes/setup.py — the in-browser first-run setup wizard.

Everything here runs BEFORE any session exists — a fresh install has no
accounts yet apart from the bootstrap admin nobody has signed into — so
these routes carry no `Depends(require_admin)`. Instead, every mutating
route (all but `GET /setup/status`) refuses once `appdb.is_setup_complete()`
is true: the wizard is a one-time front door, not a permanent unauthenticated
admin surface. See docs/setup-wizard-plan.md for the full design.

Config that needs a process restart to take effect (the model key, the
instance name, the active domain) is written to `.env` / `configs/pipeline.yaml`
here and picked up fresh on the next boot — see `api/restart.py` for how that
restart actually happens. Config that doesn't need one (the admin account,
which lives in the app database) takes effect immediately.

The clone-and-edit path (Phase 2) additionally activates a domain mid-wizard
(`/setup/domain/activate`, before finishing) and edits its fields through a
set of `/setup/ontology/*` routes that are thin, unauthenticated wrappers
around the real `api/routes/ontology.py` handlers — see the "Clone-and-edit"
section below for why calling those functions directly (not through HTTP) is
safe and avoids a second implementation of field editing.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import appdb
from ..restart import trigger_restart
from . import ontology as _ontology_routes

router = APIRouter(prefix="/setup", tags=["setup"])

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = _REPO_ROOT / ".env"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"
_PIPELINE_YAML = _REPO_ROOT / "configs" / "pipeline.yaml"

# Set once the demo path is chosen; consumed (and cleared) by api/main.py's
# lifespan handler on the next boot, after the restart this triggers.
_LOAD_DEMO_CORPUS_KEY = "setup_load_demo_corpus"


# --------------------------------------------------------------------------- #
# .env helpers — read/write without ever needing the CURRENT process's
# environment, since a just-saved value only becomes live after the restart.
# --------------------------------------------------------------------------- #
def _ensure_env_file() -> None:
    if _ENV_PATH.exists():
        return
    if _ENV_EXAMPLE.exists():
        _ENV_PATH.write_text(_ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        _ENV_PATH.write_text("", encoding="utf-8")


def _write_env(key: str, value: str) -> None:
    from dotenv import set_key
    _ensure_env_file()
    set_key(str(_ENV_PATH), key, value, quote_mode="always")


def _read_env(key: str) -> str:
    """The value as saved in .env, NOT the live process environment — a key
    saved by an earlier wizard step isn't live until the restart happens."""
    from dotenv import dotenv_values
    if not _ENV_PATH.exists():
        return ""
    return (dotenv_values(str(_ENV_PATH)).get(key) or "").strip()


def _has_real_api_key() -> bool:
    """True only for an actual key, not .env.example's untouched `sk-...`
    placeholder (a fresh .env copied from the example still carries it
    verbatim). Same placeholder check as `appdb.is_setup_complete`."""
    key = os.getenv("OPENAI_API_KEY") or _read_env("OPENAI_API_KEY")
    key = (key or "").strip()
    return bool(key) and not key.startswith("sk-...")


def _write_domain(domain: str) -> None:
    """`configs/pipeline.yaml`'s `domain:` key — preferred over an env var
    because it isn't a secret and belongs to the repo's config tree, not the
    per-deployment .env (see docs/setup-wizard-plan.md section 3.6)."""
    import yaml
    doc: dict = {}
    if _PIPELINE_YAML.exists():
        doc = yaml.safe_load(_PIPELINE_YAML.read_text(encoding="utf-8")) or {}
    doc["domain"] = domain
    _PIPELINE_YAML.parent.mkdir(parents=True, exist_ok=True)
    _PIPELINE_YAML.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _configured_domain() -> str:
    """The domain last written by `_write_domain`, straight from the file —
    NOT `pipeline.ontology.DEFAULT_DOCTYPE`, which is only re-read on the
    next process start. Used by `/setup/domain/activate` to confirm a
    domain was actually chosen before restarting into one."""
    import yaml
    if not _PIPELINE_YAML.exists():
        return ""
    doc = yaml.safe_load(_PIPELINE_YAML.read_text(encoding="utf-8")) or {}
    return str(doc.get("domain") or "").strip()


def _guard_not_complete() -> None:
    if appdb.is_setup_complete():
        raise HTTPException(
            403, "setup has already finished on this instance — use the "
                 "admin dashboard for account or schema changes.")


# --------------------------------------------------------------------------- #
@router.get("/status")
def status() -> dict:
    """Everything the wizard's frontend needs to decide which step to show,
    and everything the app shell needs to decide wizard-vs-app. Safe to call
    with no session: it returns no secrets, only presence/absence."""
    from pipeline import ontology
    domain = ontology.DEFAULT_DOCTYPE
    admins = [u for u in appdb.list_users() if u["role"] == "admin"]
    bootstrap_email = appdb.default_bootstrap_email()
    return {
        "complete": appdb.is_setup_complete(),
        "has_domain": bool(domain),
        "domain": domain,
        "available_domains": ontology.available_doctypes(),
        "has_admin": bool(admins),
        "admin_email": admins[0]["email"] if len(admins) == 1 else None,
        "bootstrap_email_pending": any(u["email"] == bootstrap_email for u in admins),
        "has_api_key": _has_real_api_key(),
        "app_name": os.getenv("APP_NAME") or _read_env("APP_NAME") or "Verbatim",
    }


class InstanceBody(BaseModel):
    name: str


@router.post("/instance")
def set_instance(body: InstanceBody) -> dict:
    _guard_not_complete()
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name cannot be empty")
    _write_env("APP_NAME", name)
    return {"ok": True, "name": name}


class AdminBody(BaseModel):
    email: str
    name: str = ""


@router.post("/admin")
def set_admin(body: AdminBody) -> dict:
    """Create (or rename into) the wizard's chosen admin account. This is the
    one and only admin the wizard creates — see docs/setup-wizard-plan.md
    section 3.3 for why it must retire the `admin@localhost` bootstrap row
    rather than add a second admin alongside it. Takes effect immediately
    (it's an app-database write, not env config), no restart needed."""
    _guard_not_complete()
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "a valid email is required")
    name = body.name.strip() or "Administrator"
    bootstrap_email = appdb.default_bootstrap_email()
    existing = appdb.get_user(email)
    if existing and existing["email"] != bootstrap_email:
        # Re-running this step (typo fix before finishing): just update it.
        appdb.update_account(email, name=name, role="admin", verifier=True)
    elif appdb.get_user(bootstrap_email):
        # The normal path: rename the auto-created bootstrap row into the
        # real admin, so admin@localhost stops being a valid sign-in at all
        # rather than becoming a second, forgotten admin account.
        appdb.update_account(bootstrap_email, new_email=email, name=name,
                             role="admin", verifier=True)
    else:
        appdb.upsert_user(email, name, "Administrator", "admin", "All")
        appdb.update_account(email, verifier=True)
    # An admin now exists — if a real key was already sitting in .env (e.g.
    # a reset that kept it), this alone can satisfy every inferred
    # completion condition. See appdb.is_setup_complete's docstring.
    appdb.mark_setup_pending()
    return {"ok": True, "email": email}


class ApiKeyBody(BaseModel):
    key: str
    base_url: str = ""


@router.post("/api-key")
def set_api_key(body: ApiKeyBody) -> dict:
    """Live-test the key before saving it (decision: never save an untested
    key). Reuses scripts/setup.py's own tiny embedding call so there is one
    definition of "this key works", not two."""
    _guard_not_complete()
    key = body.key.strip()
    if not key:
        raise HTTPException(400, "an API key is required")
    from scripts.setup import test_model_key
    ok, message = test_model_key(key, body.base_url.strip())
    if not ok:
        raise HTTPException(400, f"could not verify this key — {message}")
    _write_env("OPENAI_API_KEY", key)
    if body.base_url.strip():
        _write_env("OPENAI_BASE_URL", body.base_url.strip())
    return {"ok": True, "message": message}


@router.get("/domains")
def list_domains() -> dict:
    from pipeline import ontology
    return {"domains": ontology.available_doctypes()}


@router.post("/domain/demo")
def choose_demo_domain() -> dict:
    """The zero-authoring path: whichever domain the bundled sample corpus
    demonstrates (examples/manifest.py), plus that corpus loaded
    automatically after the restart (see api/main.py's lifespan handler,
    which reads and clears the flag set here)."""
    _guard_not_complete()
    from examples.manifest import DOMAIN as demo_domain
    from pipeline import ontology
    if demo_domain not in ontology.available_doctypes():
        raise HTTPException(
            409, f"this checkout does not ship the {demo_domain!r} demo domain")
    _write_domain(demo_domain)
    appdb.set_setting(_LOAD_DEMO_CORPUS_KEY, "1")
    return {"ok": True, "domain": demo_domain}


class DomainBody(BaseModel):
    domain: str


@router.post("/domain/use-existing")
def choose_existing_domain(body: DomainBody) -> dict:
    """Ready-made-as-is: pick one of the domains this checkout already ships
    a pack for, untouched. (Editing its fields first is the Phase 2
    clone-and-edit path, not this one.)"""
    _guard_not_complete()
    from pipeline import ontology
    domain = body.domain.strip()
    if domain not in ontology.available_doctypes():
        raise HTTPException(400, f"unknown domain {domain!r}")
    _write_domain(domain)
    return {"ok": True, "domain": domain}


def _wizard_admin() -> dict:
    """The `admin` dict `api/routes/ontology.py`'s handlers expect, standing
    in for `Depends(require_admin)` — there is no session yet at this point
    in the wizard (see the module docstring), only the one admin account the
    wizard's own admin step already created. Calling those handler functions
    directly (not through HTTP) means this dict is the only thing that needs
    faking; every validation/persistence call underneath is the exact same
    code the authenticated `/ontology/*` routes use."""
    admins = [u for u in appdb.list_users() if u["role"] == "admin"]
    if not admins:
        raise HTTPException(400, "create the admin account first")
    return admins[0]


@router.post("/domain/activate")
def activate_domain() -> dict:
    """Restart now so the domain just written by /setup/domain/demo or
    /setup/domain/use-existing takes effect in THIS process before the
    wizard continues. Only the clone-and-edit path needs this: it edits the
    newly active domain's live fields (see /setup/ontology/* below) before
    finishing, so the domain has to actually be loaded first. The demo and
    ready-made-as-is paths skip this and restart once, at /setup/finish."""
    _guard_not_complete()
    domain = _configured_domain()
    if not domain:
        raise HTTPException(409, "choose a domain first")
    trigger_restart()
    return {"ok": True, "restarting": True}


# --------------------------------------------------------------------------- #
# Clone-and-edit: a thin, unauthenticated passthrough onto the real, already-
# tested /ontology/* handlers (api/routes/ontology.py) — same validation, same
# storage, same drift gate, just called as plain functions with a stand-in
# admin dict instead of through a session (see _wizard_admin above). This is
# what lets the wizard's field-editing screen (P2.2) be "a styled wrapper
# around the real endpoints" per docs/setup-wizard-plan.md section 3.2/P2.1,
# rather than a second implementation of field editing. Every route still
# 403s once setup is complete, same as the rest of this file.
# --------------------------------------------------------------------------- #
@router.get("/ontology")
def wizard_get_ontology() -> dict:
    _guard_not_complete()
    return _ontology_routes.get_ontology(user=_wizard_admin())


@router.post("/ontology/field")
def wizard_add_field(body: _ontology_routes.FieldBody) -> dict:
    _guard_not_complete()
    return _ontology_routes.add_field(body, admin=_wizard_admin())


@router.put("/ontology/field/{category}/{key}")
def wizard_edit_field(category: str, key: str, body: _ontology_routes.EditBody) -> dict:
    _guard_not_complete()
    return _ontology_routes.edit_field(category, key, body, admin=_wizard_admin())


@router.delete("/ontology/field/{category}/{key}")
def wizard_delete_field(category: str, key: str) -> dict:
    _guard_not_complete()
    return _ontology_routes.delete_field(category, key, admin=_wizard_admin())


@router.put("/ontology/sensitivity")
def wizard_set_sensitivity(body: _ontology_routes.SensitivityBody) -> dict:
    _guard_not_complete()
    return _ontology_routes.set_sensitivity(body, admin=_wizard_admin())


# --------------------------------------------------------------------------- #
# Build from scratch (Phase 3): a plain-language description drafts a field
# list (AI, content only), the user edits it in the browser (plain add/edit/
# delete, held client-side — no active domain exists yet to persist an
# overlay onto), then this generates the four real config files and
# activates the result. See pipeline/schema_gen.py's module docstring for
# the content-vs-structure design and exactly what each toggle needs.
# --------------------------------------------------------------------------- #
class SchemaDraftBody(BaseModel):
    description: str


@router.post("/schema/draft")
async def draft_schema(body: SchemaDraftBody) -> dict:
    """One real model call — costs a small amount of money, same posture as
    /setup/api-key's test call. Never saves anything: the result is edited
    (or discarded) entirely in the browser."""
    _guard_not_complete()
    if not body.description.strip():
        raise HTTPException(400, "describe what these documents are first")
    from pipeline.schema_gen import draft_fields
    try:
        fields = await draft_fields(body.description.strip())
    except Exception as exc:  # noqa: BLE001 — surfaced as a plain message, not a 500
        raise HTTPException(400, f"could not draft a schema — {exc}") from exc
    if not fields:
        raise HTTPException(
            400, "the model didn't return any usable fields — try describing "
                 "the documents in more detail")
    return {"fields": [
        {"key": f.key, "title": f.title, "type": f.type, "hint": f.hint,
         "values": list(f.values), "category": f.category,
         "confidential": f.confidential}
        for f in fields
    ]}


class ScratchFieldBody(BaseModel):
    key: str
    title: str
    type: str = "text"
    hint: str = ""
    values: list[str] | None = None
    category: str = "details"
    multiplicity: int = 1
    confidential: bool = False


class AmendmentToggleBody(BaseModel):
    enabled: bool = False
    document_types: list[str] | None = None


class PartyToggleBody(BaseModel):
    enabled: bool = False
    field_keys: list[str] = []


class FromScratchBody(BaseModel):
    domain: str
    fields: list[ScratchFieldBody]
    amendment: AmendmentToggleBody = AmendmentToggleBody()
    party: PartyToggleBody = PartyToggleBody()


def _domain_config_paths(domain: str) -> list[Path]:
    configs = _REPO_ROOT / "configs"
    return [
        configs / "analyzers" / domain / "analyzer.yaml",
        configs / "ontology" / f"{domain}.yaml",
        configs / "packs" / f"{domain}.yaml",
        configs / "views" / f"{domain}_ops.yaml",
    ]


def _write_generated_domain(gd) -> None:
    from pipeline.schema_gen import dump_yaml
    paths = _domain_config_paths(gd.domain)
    for p, doc in zip(paths, (gd.analyzer, gd.ontology, gd.pack, gd.ops_view)):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(dump_yaml(doc), encoding="utf-8")


def _delete_generated_domain(domain: str) -> None:
    """Undo `_write_generated_domain` — called when validation below fails,
    so a rejected attempt never leaves a half-written domain that
    `ontology.available_doctypes()` would then list."""
    import shutil
    analyzer_dir, ontology_path, pack_path, view_path = _domain_config_paths(domain)
    shutil.rmtree(analyzer_dir.parent, ignore_errors=True)
    for p in (ontology_path, pack_path, view_path):
        p.unlink(missing_ok=True)


def _validate_generated_domain(domain: str) -> str | None:
    """Run the SAME two live validators `scripts/setup.py:check_configs`
    does. Every relevant cache is cleared first — a retry after fixing one
    field must not validate against a stale read of what was here before
    (see pipeline/schema_gen.py's module docstring)."""
    from pipeline.extraction.loader import load_all
    from pipeline.extraction.pack import load as load_pack
    from pipeline.kb.opsview_spec import load as load_view
    from pipeline.kb.opsview_spec import party_roles
    load_pack.cache_clear()
    party_roles.cache_clear()
    try:
        load_all(refresh=True)
        load_pack(domain)
        load_view(doctype=domain)
    except Exception as exc:  # noqa: BLE001 — PackError/OpsViewError/ConfigError, all readable
        return str(exc)
    return None


@router.post("/domain/from-scratch")
def build_from_scratch(body: FromScratchBody) -> dict:
    """Generate, validate, and activate a brand-new domain. Validation
    failures are reported once, plainly, rather than blindly retried:
    unlike a freely-generated file, every structural piece here is a fixed
    template (docs/setup-wizard-plan.md decision #12), so the only failure
    mode is a bad input (a duplicate key, a party role naming a field that
    doesn't exist) — retrying the identical template against the identical
    input would fail identically every time. Decision #13's retry budget
    was written for genuinely non-deterministic generation; this isn't."""
    _guard_not_complete()
    from pipeline import schema_gen as sg

    draft = [sg.DraftField(
        key=f.key, title=f.title, type=f.type, hint=f.hint,
        values=tuple(f.values or []), category=f.category,
        multiplicity=f.multiplicity, confidential=f.confidential,
    ) for f in body.fields]
    amendment = sg.AmendmentChainToggle(
        enabled=body.amendment.enabled,
        document_types=tuple(body.amendment.document_types or sg.DEFAULT_DOCUMENT_TYPES))
    party = sg.PartyMatchingToggle(
        enabled=body.party.enabled, field_keys=tuple(body.party.field_keys))

    try:
        gd = sg.generate(body.domain, draft, amendment=amendment, party=party)
    except sg.SchemaGenError as exc:
        raise HTTPException(400, str(exc)) from exc

    _write_generated_domain(gd)
    error = _validate_generated_domain(gd.domain)
    if error:
        _delete_generated_domain(gd.domain)
        raise HTTPException(400, f"this schema doesn't validate yet — {error}")

    _write_domain(gd.domain)
    return {"ok": True, "domain": gd.domain}


@router.post("/finish")
def finish() -> dict:
    """The wizard's last step: hard-check the things that must be true, mark
    setup complete, and restart. Checked here rather than trusted from
    earlier steps, because this is the one place a half-finished setup would
    otherwise go live."""
    _guard_not_complete()
    from pipeline import ontology
    if not ontology.DEFAULT_DOCTYPE:
        raise HTTPException(400, "no domain chosen yet")
    if not _has_real_api_key():
        raise HTTPException(400, "no working model API key saved yet")
    admins = [u for u in appdb.list_users() if u["role"] == "admin"]
    if not admins:
        raise HTTPException(400, "no admin account created yet")
    bootstrap_email = appdb.default_bootstrap_email()
    if any(u["email"] == bootstrap_email for u in admins):
        # Hard finish-line check (docs/setup-wizard-plan.md section 3.3):
        # sign-in is passwordless, so a still-live admin@localhost is a
        # documented, public backdoor, not a cosmetic loose end.
        raise HTTPException(
            400, f"the default {bootstrap_email!r} account is still active — "
                 f"finish the admin step first")
    appdb.mark_setup_complete()
    trigger_restart()
    return {"ok": True, "restarting": True}
