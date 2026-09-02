"""setup.py — get a fresh checkout from clone to a working instance.

    python -m scripts.setup            # set up this instance
    python -m scripts.setup --check    # report only, change nothing

Runs seven checks in dependency order and stops at the first one that cannot
continue, so the output names the single thing to fix rather than a wall of
consequences. Everything here is free: no model calls, no tokens. Pass
``--check-models`` to add one tiny embedding request that proves the API key
and endpoint actually work, which costs a fraction of a cent.

Safe to re-run. Nothing is overwritten: an existing .env is left alone, the
database and container are created only if absent, and the bootstrap admin is
created only when there are no accounts at all.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

OK, WARN, FAIL = "  ok  ", " warn ", " FAIL "


class Halt(Exception):
    """A check failed in a way that makes every later check meaningless."""


def say(status: str, title: str, detail: str = "") -> None:
    print(f"[{status}] {title}")
    for line in detail.splitlines():
        if line.strip():
            print(f"         {line}")


# --------------------------------------------------------------------------- #
# 1. Python
# --------------------------------------------------------------------------- #
def check_python() -> None:
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 11):
        say(FAIL, f"Python {major}.{minor}",
            "This project needs Python 3.11 or newer.")
        raise Halt
    say(OK, f"Python {major}.{minor}")


# --------------------------------------------------------------------------- #
# 2. Dependencies
# --------------------------------------------------------------------------- #
def check_dependencies() -> None:
    missing = []
    for mod, hint in (("fastapi", "fastapi"), ("openai", "openai"),
                      ("azure.cosmos", "azure-cosmos"), ("yaml", "pyyaml"),
                      ("pypdfium2", "pypdfium2"), ("pydantic", "pydantic")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(hint)
    if missing:
        say(FAIL, "Python dependencies",
            f"Missing: {', '.join(missing)}\n"
            "Install them with:  pip install -r requirements.txt")
        raise Halt
    say(OK, "Python dependencies")


# --------------------------------------------------------------------------- #
# 3. Configuration
# --------------------------------------------------------------------------- #
def check_env(*, apply: bool) -> bool:
    """Where this instance's settings come from, and whether a model key is set.

    A checkout reads .env. A container is handed its settings by whatever
    started it and has no .env at all, which is correct and must not be treated
    as a problem. So the check is "are the settings present", not "does the file
    exist".
    """
    from dotenv import load_dotenv

    env, example = ROOT / ".env", ROOT / ".env.example"
    if env.exists():
        load_dotenv(env, override=False)
        say(OK, "Configuration source", "Read from .env")
    elif os.getenv("OPENAI_API_KEY"):
        say(OK, "Configuration source",
            "Read from the environment (no .env, which is normal in a container)")
    elif not apply:
        say(FAIL, "Configuration source",
            "No .env, and nothing in the environment either.\n"
            "Run without --check to create a .env from .env.example.")
        raise Halt
    elif not example.exists():
        say(FAIL, "Configuration source",
            "No .env, nothing in the environment, and no .env.example to copy.")
        raise Halt
    else:
        shutil.copyfile(example, env)
        load_dotenv(env, override=False)
        say(OK, "Configuration source", "Created .env from .env.example")

    # A warning, not a stop. Every later step is free and independent of the
    # key: creating the database, compiling the configuration, creating the
    # first admin account. Halting here left a first-time user with an instance
    # they could not sign into, for want of something they were about to paste
    # in anyway.
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key or key.startswith("sk-..."):
        say(WARN, "Model API key",
            "OPENAI_API_KEY is not set, so reading documents and answering\n"
            "questions will fail. Everything below still works and the app\n"
            "will start. Put your key in .env (or in the container's\n"
            "environment) and restart. Any OpenAI-compatible endpoint works:\n"
            "point OPENAI_BASE_URL somewhere else to use your own.")
        return False
    where = os.getenv("OPENAI_BASE_URL", "").strip() or "api.openai.com"
    say(OK, "Model API key", f"Routing model calls to {where}.")
    return True


# --------------------------------------------------------------------------- #
# 4. Domain
# --------------------------------------------------------------------------- #
def check_domain() -> str:
    from pipeline import ontology
    packs = ontology.available_doctypes()
    if not packs:
        say(FAIL, "Domain",
            "No document pack found under configs/packs/. An instance needs\n"
            "exactly one active domain to extract against.")
        raise Halt
    try:
        domain = ontology.resolve_active_doctype(required=True)
    except RuntimeError as exc:
        say(FAIL, "Domain", str(exc))
        raise Halt from exc
    extra = "" if len(packs) == 1 else f"  (also available: " \
        f"{', '.join(p for p in packs if p != domain)})"
    say(OK, "Domain", f"{domain}{extra}")
    return domain


# --------------------------------------------------------------------------- #
# 5. Configuration compiles
# --------------------------------------------------------------------------- #
def check_configs(domain: str) -> None:
    from pipeline.extraction.pack import load as load_pack
    from pipeline.kb.opsview_spec import load as load_view
    try:
        pack = load_pack(domain)
        view = load_view(doctype=domain)
    except Exception as exc:  # noqa: BLE001 — any config error is the same problem
        say(FAIL, "Configuration", f"{type(exc).__name__}: {exc}")
        raise Halt from exc
    say(OK, "Configuration",
        f"{len(pack.by_label)} node labels, {len(view.fields)} schema fields "
        f"in {len(view.category_titles)} categories.")
    _check_silent_misconfigurations(domain)


def _check_silent_misconfigurations(domain: str) -> None:
    """Two config mistakes that produce no error and no wrong-looking output.

    Both have happened. A field role pointing at a name the domain does not use
    makes the amendment chain order on nothing, which is indistinguishable from
    a corpus that has no amendments. An access class naming a fact label the
    pack does not declare makes redaction inert, which is indistinguishable
    from a corpus with nothing confidential in it. Neither raises, so the only
    way to catch them is to look on purpose.
    """
    from pipeline.kb.opsview_spec import missing_field_roles
    missing = missing_field_roles(domain)
    if missing:
        say(WARN, "Document chain",
            "these roles read a field this domain does not declare, so the "
            "amendment chain cannot order on them: "
            + ", ".join(f"{r} -> {f!r}" for r, f in sorted(missing.items()))
            + ". Declare document_family.field_roles in the view.")
    else:
        say(OK, "Document chain", "every chain role maps to a declared field.")

    from api.rag.policy import unmatched_labels
    unmatched = unmatched_labels()
    if unmatched:
        say(WARN, "Access policy",
            "these sensitivity classes name labels the pack does not declare, "
            "so their redaction is inert: "
            + "; ".join(f"{c}: {', '.join(labs)}"
                        for c, labs in sorted(unmatched.items()))
            + ". Fix configs/policy/sensitivity.<domain>.yaml.")
    else:
        say(OK, "Access policy", "every restricted label exists in the pack.")


# --------------------------------------------------------------------------- #
# 6. Knowledge store
# --------------------------------------------------------------------------- #
def check_store(*, apply: bool) -> None:
    from pipeline.config import Config
    from pipeline.store.client import CosmosStore
    cfg = Config.load()
    where = f"{cfg.cosmos_uri} -> {cfg.cosmos_db}/{cfg.cosmos_container}"
    hint = ("Check COSMOS_URI and COSMOS_KEY in .env. For a local setup, start "
            "the database first:  docker compose up -d cosmos")
    # Construction validates the URI and credential, so it can raise before any
    # connection is attempted. Catching only wait_ready would let a malformed
    # setting escape as a traceback, which is the exact failure this command
    # exists to replace with a sentence.
    try:
        store = CosmosStore(cfg)
        store.wait_ready(timeout=20 if not apply else 120)
    except Exception as exc:  # noqa: BLE001 — every failure here means the same thing
        say(FAIL, "Knowledge store",
            f"{where}\n{type(exc).__name__}: {exc}\n{hint}")
        raise Halt from exc
    if not apply:
        say(OK, "Knowledge store", where)
        return
    try:
        store.ensure()
    except Exception as exc:  # noqa: BLE001
        say(FAIL, "Knowledge store",
            f"{where}\ncould not create the database or container\n"
            f"{type(exc).__name__}: {exc}\n{hint}")
        raise Halt from exc
    say(OK, "Knowledge store", f"{where}  (database and container ready)")


# --------------------------------------------------------------------------- #
# 7. Admin account
# --------------------------------------------------------------------------- #
def check_account(*, apply: bool) -> str | None:
    from api import appdb
    from pipeline.config import Config
    Config.load().storage_root.mkdir(parents=True, exist_ok=True)
    if not apply:
        try:
            users = appdb.list_users()
        except Exception:  # noqa: BLE001 — no database file yet
            users = []
        if users:
            admins = [u["email"] for u in users if u["role"] == "admin"]
            say(OK, "Accounts", f"{len(users)} account(s), admin: "
                                f"{', '.join(admins) or 'none'}")
        else:
            say(WARN, "Accounts", "None yet. Run without --check to create the first admin.")
        return None
    appdb.init()
    admins = [u["email"] for u in appdb.list_users() if u["role"] == "admin"]
    say(OK, "Accounts", f"Sign in as: {admins[0] if admins else '(none)'}")
    return admins[0] if admins else None


# --------------------------------------------------------------------------- #
# Optional: one paid call that proves the model endpoint answers
# --------------------------------------------------------------------------- #
def test_model_key(api_key: str, base_url: str = "", embed_model: str = "") -> tuple[bool, str]:
    """One tiny embedding request that proves a key/endpoint pair actually
    works. Pure and side-effect-free apart from that one call: takes the
    credentials as arguments rather than reading `Config.load()`, so a caller
    with a freshly-typed, not-yet-saved key (the setup wizard's API-key step,
    ``POST /setup/api-key``) can test it before writing it anywhere. Never
    raises — the bool says whether it worked, the string is what to show
    either way. ``check_models`` below is the CLI's thin wrapper around it."""
    from openai import OpenAI

    from pipeline.config import OPENAI_DEFAULT_BASE_URL, Config
    model = embed_model or Config.load().embed_model
    try:
        r = OpenAI(api_key=api_key, base_url=base_url or OPENAI_DEFAULT_BASE_URL) \
            .embeddings.create(model=model, input="ping")
    except Exception as exc:  # noqa: BLE001 — the message is the useful part
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"{model} answered with {len(r.data[0].embedding)} dimensions."


def check_models() -> None:
    from pipeline.config import Config
    cfg = Config.load()
    # base_url is always explicit: left to the SDK it reads OPENAI_BASE_URL from
    # the environment, and a .env line left blank is present-but-empty.
    ok, msg = test_model_key(
        cfg.openai_embed_api_key or cfg.openai_api_key,
        cfg.openai_embed_base_url or cfg.openai_base_url,
        cfg.embed_model,
    )
    if not ok:
        say(FAIL, "Model endpoint", msg)
        raise Halt
    say(OK, "Model endpoint", msg)


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.setup",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="report only, create and write nothing")
    ap.add_argument("--check-models", action="store_true",
                    help="also send one small embedding request (costs a fraction of a cent)")
    args = ap.parse_args(argv)
    apply = not args.check

    print(f"\n{'Checking' if args.check else 'Setting up'} this instance\n")
    try:
        check_python()
        check_dependencies()
        have_key = check_env(apply=apply)
        domain = check_domain()
        check_configs(domain)
        check_store(apply=apply)
        admin = check_account(apply=apply)
        if args.check_models:
            check_models()
    except Halt:
        print("\nStopped at the first blocking problem. Fix it and run this again.\n")
        return 1

    if args.check:
        print("\nAll checks passed.\n" if have_key else
              "\nReady apart from the model key noted above.\n")
        return 0

    print(f"""
Ready. Start it with:

    docker compose up -d          everything, on http://localhost:8000

or, without Docker:

    python -m uvicorn api.main:app --reload --port 8000
    cd web && npm install && npm run dev

Then sign in as {admin or 'your admin account'} and upload a PDF from the
Admin tab. Extraction starts on upload and the document becomes searchable
as soon as it finishes.
""")
    if not have_key:
        print("Still missing: OPENAI_API_KEY. The app starts and you can sign "
              "in, but nothing\ncan be read or answered until you set it and "
              "restart.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
