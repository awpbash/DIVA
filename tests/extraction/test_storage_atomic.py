"""Unit tests for pipeline/storage.py atomic write helpers.

Contract:
  - atomic_write_json lands the full payload or nothing (tmp + os.replace)
  - overwriting an existing file works (Windows os.replace semantics)
  - no *.tmp siblings are left behind after a successful write
"""
from __future__ import annotations

import json

from pipeline.storage import atomic_write_json, atomic_write_text


def test_atomic_write_json_roundtrip(tmp_path):
    p = tmp_path / "out.json"
    atomic_write_json(p, {"a": 1, "b": ["x", "y"]})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "b": ["x", "y"]}
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_json_overwrites_existing(tmp_path):
    p = tmp_path / "out.json"
    p.write_text(json.dumps({"old": True}), encoding="utf-8")
    atomic_write_json(p, {"new": True}, indent=None)
    assert json.loads(p.read_text(encoding="utf-8")) == {"new": True}
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_text_plain(tmp_path):
    p = tmp_path / "note.txt"
    atomic_write_text(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"
    assert list(tmp_path.glob("*.tmp")) == []
