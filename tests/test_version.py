"""The release version is declared twice, so pin the two together.

``api/__init__.__version__`` is what the running API reports at ``GET /branding``
and in the OpenAPI document. ``pyproject.toml`` is what packaging tools read.
A release where those disagree tells two different stories about what is
deployed, which is the kind of thing nobody notices until it matters.
"""
from __future__ import annotations

import re
from pathlib import Path

import api

ROOT = Path(__file__).resolve().parents[1]


def test_version_matches_pyproject():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml has no version"
    assert m.group(1) == api.__version__, (
        f"pyproject.toml says {m.group(1)}, api/__init__.py says {api.__version__}")


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-+].+)?", api.__version__), api.__version__
