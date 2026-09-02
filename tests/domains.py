"""Which domains this checkout ships, and how a test declares it needs one.

Two kinds of test live in this suite and the difference matters:

**Contract tests** assert things that must hold for *any* domain: the pack
compiles, every projection is a subset of the properties it projects, enums are
closed. They are parametrized over ``ALL`` and therefore test whatever the
checkout happens to ship. Files named ``*_contract.py``.

**Example-domain tests** assert hand-checked facts about one specific domain:
that this analyzer declares these roles, that this view has these categories.
That specificity is the point, so they name their domain, and they skip when it
is absent rather than fail.

The distinction exists because the engine ships to other people. A published
copy carries one domain, not this checkout's two, and the contract tests have to
keep working there or the drift gates arrive inert.
"""
from __future__ import annotations

import pytest

from pipeline import ontology

ALL: list[str] = ontology.available_doctypes()
AVAILABLE = frozenset(ALL)
ACTIVE: str = ontology.DEFAULT_DOCTYPE


def skip_unless(*doctypes: str) -> None:
    """Skip the whole module unless every named domain is PRESENT.

    Call it at module level BEFORE any module-level config loading, because
    that loading is what would otherwise raise during collection.
    """
    missing = [d for d in doctypes if d not in AVAILABLE]
    if missing:
        pytest.skip(f"this checkout does not ship: {', '.join(missing)}",
                    allow_module_level=True)


def requires_active(doctype: str):
    """Mark a test that asserts the behaviour of the ACTIVE domain.

    Different from ``skip_unless``: present is not the same as active. A few
    functions read the resolved domain internally rather than taking one, so a
    test of their output is a test of whichever domain is switched on. Asserting
    one domain's answers while another is active is not a failure, it is the
    wrong question, so skip instead.
    """
    return pytest.mark.skipif(
        ACTIVE != doctype,
        reason=f"asserts '{doctype}' behaviour, but '{ACTIVE}' is the active domain")
