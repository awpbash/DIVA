"""The first-run setup wizard: the boot gate, the full /setup/* flow, the
clone-and-edit ontology passthrough, and the dev/admin reset that's supposed
to bring the wizard back.

Runs against the real FastAPI app (TestClient triggers the real lifespan,
same pattern as tests/api/test_admin_intake.py) with the app database and
storage root pointed at a tmp dir, and this checkout's REAL shipped domain
(commercial_agreement — the repo ships exactly one pack, so
pipeline.ontology.DEFAULT_DOCTYPE already resolves to it via autodetection,
same as test_admin_intake.py relies on for doc_types()). No live network
calls: the model-key test is mocked, and the Cosmos step of a reset is
best-effort and degrades to a warning when nothing is listening, same as
the lifespan's own store-warmup thread already does in every other API test.

Restarts are the one real hazard here: /setup/finish, /setup/domain/activate
and /admin/dev-reset all end with a call to `os._exit(0)` on a delay in the
REAL app — which would kill the test runner itself. Every test that reaches
one of those must patch `trigger_restart` to a no-op first.
"""
from __future__ import annotations

import pytest

from api import appdb


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """TestClient against a throwaway app db + storage root, with the
    wizard's .env / configs/pipeline.yaml writes redirected into tmp_path
    so a test run can never touch this checkout's real files."""
    # Separate subdirectories: perform_reset() below deletes everything
    # under storage_root except app.db, so the fake pipeline.yaml/.env must
    # live somewhere else, exactly like the real repo (configs/ next to,
    # never inside, storage/).
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    etc = tmp_path / "etc"
    etc.mkdir()
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)   # ignore the dev's real .env
    monkeypatch.delenv("VERBATIM_DOMAIN", raising=False)

    import api.routes.setup as setup_routes
    pipeline_yaml = etc / "pipeline.yaml"
    monkeypatch.setattr(setup_routes, "_ENV_PATH", etc / ".env")
    monkeypatch.setattr(setup_routes, "_ENV_EXAMPLE", etc / ".env.example.absent")
    monkeypatch.setattr(setup_routes, "_PIPELINE_YAML", pipeline_yaml)
    # Never actually exit the test process.
    monkeypatch.setattr(setup_routes, "trigger_restart", lambda *a, **k: None)

    # scripts/reset_dev.py's domain-clearing step targets the same file the
    # wizard just wrote — real production code always means the repo's one
    # real configs/pipeline.yaml, so the test has to point both at the same
    # (here, tmp) path to exercise that interaction instead of the real one.
    import scripts.reset_dev as reset_dev
    monkeypatch.setattr(reset_dev, "_PIPELINE_YAML", pipeline_yaml)

    import api.routes.admin as admin_routes
    monkeypatch.setattr(admin_routes, "trigger_restart", lambda *a, **k: None)

    import scripts.setup as setup_cli
    monkeypatch.setattr(setup_cli, "test_model_key",
                        lambda key, base_url="", embed_model="": (True, "fake: ok"))

    from fastapi.testclient import TestClient
    from api.main import app
    with TestClient(app) as client:
        yield client


def _bootstrap_email(client) -> str:
    return client.get("/setup/status").json()["admin_email"]


def _walk_to_domain_step(client, admin_email="owner@example.com") -> None:
    """The three steps every path shares: instance name, admin, api key."""
    assert client.post("/setup/instance", json={"name": "Acme Contracts"}).status_code == 200
    assert client.post("/setup/admin", json={"email": admin_email, "name": "Owner"}).status_code == 200
    r = client.post("/setup/api-key", json={"key": "sk-testkey-not-real"})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# The boot gate
# --------------------------------------------------------------------------- #
def test_gate_blocks_gated_routes_before_setup(rig):
    r = rig.get("/documents")
    assert r.status_code == 503
    assert r.json()["setup_required"] is True


def test_gate_exempts_setup_health_and_branding(rig):
    for path in ("/setup/status", "/healthz", "/branding"):
        assert rig.get(path).status_code == 200, path


def test_gate_opens_once_marked_complete(rig):
    appdb.mark_setup_complete()
    r = rig.get("/documents")
    assert r.status_code != 503 or not r.json().get("setup_required")


# --------------------------------------------------------------------------- #
# The full wizard flow (demo path) — instance -> admin -> key -> domain -> finish
# --------------------------------------------------------------------------- #
def test_full_wizard_demo_path_happy_path(rig):
    bootstrap = _bootstrap_email(rig)
    assert rig.get("/setup/status").json()["bootstrap_email_pending"] is True

    _walk_to_domain_step(rig)
    r = rig.post("/setup/domain/demo")
    assert r.status_code == 200, r.text
    assert appdb.get_setting("setup_load_demo_corpus") == "1"

    status = rig.get("/setup/status").json()
    assert status["has_api_key"] is True
    assert status["bootstrap_email_pending"] is False   # renamed by /setup/admin

    r = rig.post("/setup/finish")
    assert r.status_code == 200, r.text
    assert appdb.is_setup_complete() is True

    # The bootstrap account is gone, not merely joined by a second admin.
    assert appdb.get_user(bootstrap) is None
    admins = [u for u in appdb.list_users() if u["role"] == "admin"]
    assert [a["email"] for a in admins] == ["owner@example.com"]

    # A one-time front door: every mutating route 403s from here on.
    assert rig.post("/setup/instance", json={"name": "x"}).status_code == 403
    assert rig.post("/setup/finish").status_code == 403


def test_admin_step_marks_pending_so_a_real_key_cant_self_complete(rig, monkeypatch):
    # Simulates a reset that wiped accounts but kept the key (the default):
    # the domain still autodetects, so once /setup/admin creates a real
    # admin, every inferred condition would otherwise already be true.
    for u in appdb.list_users():
        appdb.delete_user(u["email"])
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-from-before-the-wizard")
    assert appdb.is_setup_complete() is False

    assert rig.post("/setup/instance", json={"name": "Acme"}).status_code == 200
    assert rig.post("/setup/admin", json={"email": "owner@example.com"}).status_code == 200
    assert appdb.get_setting("setup_pending") == "1"
    assert appdb.is_setup_complete() is False

    r = rig.post("/setup/api-key", json={"key": "sk-testkey-not-real"})
    assert r.status_code == 200, r.text
    assert rig.post("/setup/domain/demo").status_code == 200
    assert rig.post("/setup/finish").status_code == 200
    assert appdb.is_setup_complete() is True


def test_abandoning_the_wizard_before_admin_leaves_it_usable_by_hand(rig, monkeypatch):
    # Naming the instance (or any read-only call) must NOT latch pending —
    # only /setup/admin does. Someone who tries the wizard, backs out before
    # creating an admin, and finishes configuring by hand must not get stuck.
    assert rig.post("/setup/instance", json={"name": "Acme"}).status_code == 200
    assert appdb.get_setting("setup_pending") is None
    monkeypatch.setenv("OPENAI_API_KEY", "sk-hand-configured-key")
    assert appdb.is_setup_complete() is True


def test_finish_requires_retiring_the_bootstrap_admin(rig):
    # Admin step skipped: admin@localhost (or BOOTSTRAP_ADMIN_EMAIL) is still
    # the only admin — finish must refuse rather than ship a live backdoor.
    assert rig.post("/setup/instance", json={"name": "Acme"}).status_code == 200
    assert rig.post("/setup/api-key", json={"key": "sk-testkey"}).status_code == 200
    assert rig.post("/setup/domain/demo").status_code == 200
    r = rig.post("/setup/finish")
    assert r.status_code == 400
    assert "admin" in r.json()["detail"].lower()


def test_finish_requires_a_real_api_key(rig):
    assert rig.post("/setup/instance", json={"name": "Acme"}).status_code == 200
    assert rig.post("/setup/admin", json={"email": "a@b.com"}).status_code == 200
    assert rig.post("/setup/domain/demo").status_code == 200
    r = rig.post("/setup/finish")
    assert r.status_code == 400
    assert "key" in r.json()["detail"].lower()


def test_api_key_is_never_saved_when_the_test_call_fails(rig, monkeypatch):
    import scripts.setup as setup_cli
    monkeypatch.setattr(setup_cli, "test_model_key",
                        lambda key, base_url="", embed_model="": (False, "boom"))
    r = rig.post("/setup/api-key", json={"key": "sk-whatever"})
    assert r.status_code == 400
    assert rig.get("/setup/status").json()["has_api_key"] is False


# --------------------------------------------------------------------------- #
# Clone-and-edit: /setup/ontology/* is a passthrough onto the real handlers
# --------------------------------------------------------------------------- #
def test_setup_ontology_passthrough_reads_and_edits_the_real_schema(rig):
    _walk_to_domain_step(rig)
    real_domain = rig.get("/setup/domains").json()["domains"][0]
    assert rig.post("/setup/domain/use-existing", json={"domain": real_domain}).status_code == 200

    before = rig.get("/setup/ontology").json()
    assert before["categories"], "the real shipped domain should have fields"
    category = before["categories"][0]["key"]

    add = rig.post("/setup/ontology/field", json={
        "category": category, "key": "wizard_probe_field",
        "title": "Wizard Probe", "type": "text", "hint": "test only",
    })
    assert add.status_code == 200, add.text

    after = rig.get("/setup/ontology").json()
    cat = next(c for c in after["categories"] if c["key"] == category)
    added = next(f for f in cat["fields"] if f["key"] == "wizard_probe_field")
    assert added["user_field"] is True

    edit = rig.put(f"/setup/ontology/field/{category}/wizard_probe_field",
                   json={"title": "Renamed Probe"})
    assert edit.status_code == 200
    sens = rig.put("/setup/ontology/sensitivity", json={"category": category, "level": "confidential"})
    assert sens.status_code == 200

    delete = rig.delete(f"/setup/ontology/field/{category}/wizard_probe_field")
    assert delete.status_code == 200
    after_delete = rig.get("/setup/ontology").json()
    cat2 = next(c for c in after_delete["categories"] if c["key"] == category)
    assert all(f["key"] != "wizard_probe_field" for f in cat2["fields"])


def test_setup_ontology_routes_403_once_complete(rig):
    _walk_to_domain_step(rig)
    real_domain = rig.get("/setup/domains").json()["domains"][0]
    rig.post("/setup/domain/use-existing", json={"domain": real_domain})
    assert rig.post("/setup/finish").status_code == 200
    assert rig.get("/setup/ontology").status_code == 403


def test_activate_domain_requires_a_domain_first(rig):
    assert rig.post("/setup/domain/activate").status_code == 409


# --------------------------------------------------------------------------- #
# Reset to a clean slate — the developer escape hatch
# --------------------------------------------------------------------------- #
def test_perform_reset_wipes_state_and_the_wizard_reopens(rig, monkeypatch):
    _walk_to_domain_step(rig)
    real_domain = rig.get("/setup/domains").json()["domains"][0]
    rig.post("/setup/domain/use-existing", json={"domain": real_domain})
    assert rig.post("/setup/finish").status_code == 200
    assert appdb.is_setup_complete() is True

    # Simulate the key genuinely being live in the process environment (in
    # production .env's key becomes exactly this at boot) and NOT wiped by
    # the reset below — the scenario that matters, since this repo's
    # shipped domain autodetects regardless of the domain-clear, and a
    # fresh admin regrows for free on the next boot.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key-kept-across-the-reset")

    from scripts.reset_dev import perform_reset
    report = perform_reset()

    assert appdb.list_users() == []          # every row gone, not just re-queryable
    assert appdb.get_setting("setup_complete") is None
    assert appdb.get_setting("setup_pending") == "1"
    assert report["domain_cleared"] is True

    appdb.init()   # simulates the next boot: regrows a fresh default admin
    assert any(u["role"] == "admin" for u in appdb.list_users())
    assert appdb.is_setup_complete() is False


def test_dev_reset_route_disabled_by_default(rig, monkeypatch):
    monkeypatch.delenv("VERBATIM_ALLOW_RESET", raising=False)
    _walk_to_domain_step(rig)
    real_domain = rig.get("/setup/domains").json()["domains"][0]
    rig.post("/setup/domain/use-existing", json={"domain": real_domain})
    rig.post("/setup/finish")
    token = appdb.create_session("owner@example.com")
    headers = {"X-User-Token": token}

    status = rig.get("/admin/dev-reset", headers=headers)
    assert status.status_code == 200
    assert status.json()["enabled"] is False

    r = rig.post("/admin/dev-reset", json={"confirm": "RESET"}, headers=headers)
    assert r.status_code == 403


def test_dev_reset_route_wipes_and_restarts_when_enabled(rig, monkeypatch):
    monkeypatch.setenv("VERBATIM_ALLOW_RESET", "1")
    _walk_to_domain_step(rig)
    real_domain = rig.get("/setup/domains").json()["domains"][0]
    rig.post("/setup/domain/use-existing", json={"domain": real_domain})
    rig.post("/setup/finish")
    token = appdb.create_session("owner@example.com")
    headers = {"X-User-Token": token}

    assert rig.get("/admin/dev-reset", headers=headers).json()["enabled"] is True

    bad = rig.post("/admin/dev-reset", json={"confirm": "nope"}, headers=headers)
    assert bad.status_code == 400

    r = rig.post("/admin/dev-reset", json={"confirm": "reset"}, headers=headers)  # case-insensitive
    assert r.status_code == 200, r.text
    assert r.json()["report"]["domain_cleared"] is True


# --------------------------------------------------------------------------- #
# Build from scratch (Phase 3)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def scratch_dirs(rig, monkeypatch, tmp_path):
    """Layered on `rig`: isolates the FOUR generated-domain paths (analyzer /
    ontology / pack / ops-view) into a throwaway repo root, seeded with the
    real _base.yaml / _universal analyzer / analyzer schema / pipeline.yaml
    so `extends: _base` / `extends: _universal` still resolve. Only used by
    the from-scratch tests below — every other test in this file keeps
    reading the real commercial_agreement domain untouched (see `rig`'s own
    docstring), so this must never be part of `rig` itself."""
    import shutil
    from pathlib import Path

    import api.routes.setup as setup_routes
    import pipeline.extraction.loader as loader_mod
    import pipeline.extraction.pack as pack_mod
    import pipeline.kb.opsview_spec as opsview_mod

    repo = Path(__file__).resolve().parents[2]
    fake_root = tmp_path / "fake_repo"
    configs = fake_root / "configs"
    (configs / "analyzers").mkdir(parents=True)
    (configs / "ontology").mkdir(parents=True)
    (configs / "packs").mkdir(parents=True)
    (configs / "views").mkdir(parents=True)
    (configs / "schemas").mkdir(parents=True)
    shutil.copytree(repo / "configs" / "analyzers" / "_universal",
                    configs / "analyzers" / "_universal")
    shutil.copy(repo / "configs" / "packs" / "_base.yaml", configs / "packs" / "_base.yaml")
    shutil.copy(repo / "configs" / "schemas" / "analyzer.schema.json",
               configs / "schemas" / "analyzer.schema.json")
    shutil.copy(repo / "configs" / "pipeline.yaml", configs / "pipeline.yaml")

    monkeypatch.setattr(setup_routes, "_REPO_ROOT", fake_root)
    monkeypatch.setattr(loader_mod, "CONFIGS_DIR", configs)
    monkeypatch.setattr(loader_mod, "_REPO_ROOT", fake_root)
    monkeypatch.setattr(pack_mod, "PACKS_DIR", configs / "packs")
    monkeypatch.setattr(opsview_mod, "_VIEWS_DIR", configs / "views")
    pack_mod.load.cache_clear()
    opsview_mod.party_roles.cache_clear()
    loader_mod.load_all(refresh=True)
    yield rig


def test_draft_schema_requires_a_description(rig):
    r = rig.post("/setup/schema/draft", json={"description": "  "})
    assert r.status_code == 400


def test_draft_schema_uses_the_mocked_model_call(rig, monkeypatch):
    async def fake_draft(description: str):
        from pipeline.schema_gen import DraftField
        return [DraftField(key="rent_amount", title="Rent Amount", type="value",
                           hint="the monthly rent", category="commercial")]
    import pipeline.schema_gen as sg
    monkeypatch.setattr(sg, "draft_fields", fake_draft)

    r = rig.post("/setup/schema/draft", json={"description": "lease agreements"})
    assert r.status_code == 200, r.text
    fields = r.json()["fields"]
    assert fields == [{"key": "rent_amount", "title": "Rent Amount", "type": "value",
                       "hint": "the monthly rent", "values": [], "category": "commercial",
                       "confidential": False}]


def test_from_scratch_rejects_a_bad_field_list_before_writing_anything(scratch_dirs):
    r = scratch_dirs.post("/setup/domain/from-scratch", json={
        "domain": "bad_domain", "fields": [],
    })
    assert r.status_code == 400
    import api.routes.setup as setup_routes
    assert not any(p.exists() for p in setup_routes._domain_config_paths("bad_domain"))


def test_from_scratch_full_pipeline_no_toggles(scratch_dirs):
    r = scratch_dirs.post("/setup/domain/from-scratch", json={
        "domain": "wizard test domain!",
        "fields": [{"key": "reference_number", "title": "Reference Number",
                   "type": "text", "hint": "the internal reference number"}],
    })
    assert r.status_code == 200, r.text
    domain = r.json()["domain"]
    assert domain == "wizard_test_domain"

    from pipeline.extraction.pack import load as load_pack
    from pipeline.kb.opsview_spec import load as load_view
    load_pack(domain)   # must not raise — this IS the validation
    view = load_view(doctype=domain)
    assert len(view.fields) == 1

    # /setup/domain/activate's own file target — proves from-scratch and
    # clone-and-edit write the domain declaration through the same path.
    import api.routes.setup as setup_routes
    assert setup_routes._configured_domain() == domain


def test_from_scratch_full_pipeline_with_toggles(scratch_dirs):
    r = scratch_dirs.post("/setup/domain/from-scratch", json={
        "domain": "lease_domain",
        "fields": [
            {"key": "landlord_name", "title": "Landlord", "type": "text",
             "hint": "the landlord", "category": "parties"},
            {"key": "tenant_name", "title": "Tenant", "type": "text",
             "hint": "the tenant", "category": "parties"},
            {"key": "monthly_rent", "title": "Monthly Rent", "type": "value",
             "hint": "the monthly rent", "category": "commercial", "confidential": True},
        ],
        "amendment": {"enabled": True},
        "party": {"enabled": True, "field_keys": ["landlord_name", "tenant_name"]},
    })
    assert r.status_code == 200, r.text

    from pipeline.extraction.pack import load as load_pack
    from pipeline.kb.opsview_spec import load as load_view
    pack = load_pack("lease_domain")
    view = load_view(doctype="lease_domain")
    assert len(view.fields) == 3 + 5
    assert "Party" in pack.fact_labels
    rent = next(f for f in view.fields if f.key == "monthly_rent")
    assert rent.sensitivity == "confidential"


def test_from_scratch_cleans_up_after_a_validation_failure(scratch_dirs, monkeypatch):
    import api.routes.setup as setup_routes
    monkeypatch.setattr(setup_routes, "_validate_generated_domain",
                        lambda domain: "synthetic failure for the test")
    r = scratch_dirs.post("/setup/domain/from-scratch", json={
        "domain": "broken_domain",
        "fields": [{"key": "a", "title": "A", "type": "text"}],
    })
    assert r.status_code == 400
    assert "synthetic failure" in r.json()["detail"]
    paths = setup_routes._domain_config_paths("broken_domain")
    assert not any(p.exists() for p in paths)
    # And it must not have been declared active either.
    assert setup_routes._configured_domain() != "broken_domain"
