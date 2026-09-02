"""Rendering must not destroy the previous pages when it fails.

The defect this guards: `render_pdf` deleted every existing `p_*.png` before it
opened the PDF. Any failure after that point left the page images gone while
the `.done` stamp still matched the PDF hash and the DPI, so the next run took
the cached path, reported zero pages, and OCR failed on the missing files. The
operator's only recovery affordance re-runs that same cached path, so the
document was stuck for good.
"""
from __future__ import annotations

import json

import pytest

from pipeline.extraction import render


def _existing_pages(pages_dir, n=3):
    pages_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, n + 1):
        (pages_dir / f"p_{i:03d}.png").write_bytes(b"old page")
    return sorted(p.name for p in pages_dir.glob("p_*.png"))


def test_a_failed_render_leaves_the_previous_pages_intact(tmp_path, monkeypatch):
    pages = tmp_path / "pages"
    before = _existing_pages(pages)

    def boom(*a, **k):
        raise RuntimeError("pdfium fell over")

    monkeypatch.setattr(render.pdfium, "PdfDocument", boom)
    with pytest.raises(RuntimeError):
        render.render_pdf(tmp_path / "doc.pdf", pages, dpi=200)

    after = sorted(p.name for p in pages.glob("p_*.png"))
    assert after == before, "a failed render destroyed the usable pages"
    assert all((pages / name).read_bytes() == b"old page" for name in after)


def test_a_failed_render_leaves_no_staging_directory(tmp_path, monkeypatch):
    pages = tmp_path / "pages"
    _existing_pages(pages)
    monkeypatch.setattr(render.pdfium, "PdfDocument",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        render.render_pdf(tmp_path / "doc.pdf", pages, dpi=200)
    assert not (pages / ".render_tmp").exists()


def test_a_zero_page_pdf_fails_loudly(tmp_path, monkeypatch):
    """It used to render nothing, stamp `n_pages: 0` and report success, so
    the real problem surfaced pages later as a confusing missing-PNG error."""
    class _Empty:
        def __len__(self):
            return 0

        def close(self):
            pass

    monkeypatch.setattr(render.pdfium, "PdfDocument", lambda *a, **k: _Empty())
    with pytest.raises(ValueError, match="no pages"):
        render.render_pdf(tmp_path / "doc.pdf", tmp_path / "pages", dpi=200)


def test_a_matching_stamp_with_no_pages_is_not_a_cache_hit(tmp_path, monkeypatch):
    """The state the old bug left behind. The stamp says the render is done,
    the directory is empty, and the cached path used to believe the stamp."""
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    stamp = render._stamp_path(pages)
    stamp.write_text(json.dumps({"pdf_sha": "abc", "dpi": 200, "n_pages": 3}),
                     encoding="utf-8")
    assert render._stamp_matches(stamp, "abc", 200) is True
    assert sum(1 for _ in pages.glob("p_*.png")) == 0
