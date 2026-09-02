"""Block-sensitivity derivation (pipeline/kb/km.py) — the marker→block mapping
and the confidential-block collection that close the raw-text leak. Pure
filesystem logic over a synthetic storage tree; no graph."""
from __future__ import annotations

import json
from types import SimpleNamespace

from pipeline.kb import km
from pipeline.storage import fields_dir


def _mk_pages(root, doc_id: str) -> None:
    d = root / "pages_md" / doc_id
    d.mkdir(parents=True)
    (d / "p_001.json").write_text(json.dumps({
        "page_no": 1,
        "blocks": [
            {"text": "Recitals…", "bbox": [0.1, 0.1, 0.9, 0.2]},
            {"text": "The rate shall be S$0.28/RTh.", "bbox": [0.1, 0.3, 0.9, 0.4]},
        ],
    }), encoding="utf-8")
    (d / "p_002.json").write_text(json.dumps({
        "page_no": 2,
        "blocks": [
            {"text": "Deposit: S$100,000.", "bbox": [0.1, 0.5, 0.9, 0.6]},
        ],
    }), encoding="utf-8")


def test_block_marker_map_uses_the_global_counter(tmp_path):
    _mk_pages(tmp_path, "doc1")
    cfg = SimpleNamespace(storage_root=tmp_path)
    m = km.block_marker_map(cfg, "doc1")
    # k advances across pages: page1 has b0001/b0002, page2 starts at b0003.
    assert m["p1b0"] == "doc1:b0001"
    assert m["p1b1"] == "doc1:b0002"
    assert m["p2b0"] == "doc1:b0003"
    assert km.block_marker_map(cfg, "missing-doc") == {}


def _view(*confidential_keys: str):
    fields = [SimpleNamespace(full_key=k, sensitivity="confidential")
              for k in confidential_keys]
    fields.append(SimpleNamespace(full_key="service_scope.general_thing",
                                  sensitivity="general"))
    return SimpleNamespace(fields=fields)


def test_confidential_block_ids_from_markers_and_attached_rects(tmp_path):
    _mk_pages(tmp_path, "doc1")
    cfg = SimpleNamespace(storage_root=tmp_path)

    # Extractor citation: the confidential rate field cites marker p1b1.
    ext_dir = fields_dir(cfg)
    ext_dir.mkdir()
    (ext_dir / "doc1.json").write_text(json.dumps({
        "fields": {
            "commercial_link.rate": {"values": [{
                "value": "S$0.28/RTh",
                "evidence": [{"blocks": ["p1b1"], "snippet": "rate", "page": 1}],
            }]},
            "service_scope.general_thing": {"values": [{
                "value": "licensed software",
                "evidence": [{"blocks": ["p1b0"], "snippet": "x", "page": 1}],
            }]},
        },
    }), encoding="utf-8")

    # Reviewer attached the deposit clause (rects only — no marker) to a
    # confidential field; the rect equals page 2's block bbox exactly.
    rev = tmp_path / "review"
    rev.mkdir()
    (rev / "doc1.verified.json").write_text(json.dumps({
        "commercial_link.rate": {"added": [{
            "value": "S$100,000",
            "rects": json.dumps([{"page_no": 2, "bbox": [0.1, 0.5, 0.9, 0.6]}]),
        }]},
    }), encoding="utf-8")

    ids = km.confidential_block_ids(cfg, _view("commercial_link.rate"), "doc1")
    assert ids == ["doc1:b0002", "doc1:b0003"]   # cited rate block + attached deposit block
    # The general field's block is NOT tagged.
    assert "doc1:b0001" not in ids


def test_confidential_block_ids_empty_without_extraction(tmp_path):
    cfg = SimpleNamespace(storage_root=tmp_path)
    assert km.confidential_block_ids(cfg, _view("a.b"), "nope") == []
