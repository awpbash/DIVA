"""
render.py — PDF → per-page PNGs.

The first stage of the pipeline. Takes a PDF from ``storage/raw/<doc_id>.pdf``
and writes one PNG per page to ``storage/pages/<doc_id>/p_NNN.png`` at the
DPI declared in ``configs/pipeline.yaml: render.dpi``.

Inputs
------
* ``storage/raw/<doc_id>.pdf``
* ``configs/pipeline.yaml: render.dpi`` (and ``image_format``, currently png)

Output
------
* ``storage/pages/<doc_id>/p_NNN.png`` for N in 1..page_count
* ``storage/pages/<doc_id>/.done`` — stamp containing ``{pdf_sha, dpi}`` for
  idempotency (re-runs with unchanged inputs skip entirely).

Why this exists
---------------
The pipeline's downstream stages all consume ``page_png(doc_id, n)``. Render
is the one stage that's pure CPU and produces no LLM tokens, so keeping it
isolated lets us re-render at a different DPI without burning vision $$.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium

from ..config import Config
from ..storage import Paths, file_sha256
from . import load_all


@dataclass(frozen=True)
class RenderResult:
    doc_id: str
    n_pages: int
    cached: bool
    dpi: int


def _stamp_path(pages_dir: Path) -> Path:
    return pages_dir / ".done"


def _stamp_matches(stamp: Path, pdf_sha: str, dpi: int) -> bool:
    if not stamp.exists():
        return False
    try:
        prev = json.loads(stamp.read_text(encoding="utf-8"))
        return prev.get("pdf_sha") == pdf_sha and int(prev.get("dpi", -1)) == dpi
    except (json.JSONDecodeError, OSError):
        return False


def _write_stamp(stamp: Path, pdf_sha: str, dpi: int, n_pages: int) -> None:
    stamp.write_text(
        json.dumps({"pdf_sha": pdf_sha, "dpi": dpi, "n_pages": n_pages}),
        encoding="utf-8",
    )


def render_pdf(pdf_path: Path, pages_dir: Path, dpi: int) -> int:
    """
    Pure-ish: render ``pdf_path`` to PNGs under ``pages_dir`` at ``dpi``.
    Returns page count. Caller decides on idempotency.

    Renders into a sibling temp directory and swaps on success. The old code
    deleted the existing PNGs BEFORE opening the PDF, so any failure after that
    point (an encrypted file substituted, a disk filling up, a pdfium fault)
    left the page images gone while the ``.done`` stamp still matched. The next
    run then took the cached path, reported zero pages, and OCR failed on the
    missing PNGs forever, because the operator's only recovery affordance
    re-runs exactly the same cached path.
    """
    pages_dir.mkdir(parents=True, exist_ok=True)
    scale = dpi / 72.0   # PDF native unit is 1/72 inch
    staging = pages_dir / ".render_tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()

    try:
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            n = len(pdf)
            if n == 0:
                # A zero-page PDF used to render nothing, stamp `n_pages: 0`
                # and report success, so the failure surfaced pages later as a
                # confusing "no PNGs" error from OCR.
                raise ValueError(f"{pdf_path.name} has no pages to render")
            for i in range(n):
                page = pdf[i]
                bitmap = page.render(scale=scale)
                # Flatten to RGB: downstream OCR and the vision models want
                # opaque pages, and an alpha channel makes the PNGs bigger for
                # nothing.
                bitmap.to_pil().convert("RGB").save(staging / f"p_{i + 1:03d}.png")
                page.close()
        finally:
            pdf.close()

        # Swap only now that every page rendered. Clearing stragglers from a
        # previous render at a different DPI happens here too, so a failure
        # above leaves the previous render untouched and usable.
        for old in pages_dir.glob("p_*.png"):
            old.unlink()
        for png in staging.glob("p_*.png"):
            png.replace(pages_dir / png.name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return n


def run(cfg: Config, doc_id: str, *, force: bool = False) -> RenderResult:
    """
    Render one PDF. Idempotent: skips when ``.done`` stamp matches both the
    PDF sha and the current DPI.
    """
    paths = Paths(cfg)
    pdf_path = paths.raw_pdf(doc_id)
    if not pdf_path.exists():
        raise FileNotFoundError(f"no PDF at {pdf_path}")

    defaults, _ = load_all()
    dpi = defaults.render_dpi
    pdf_sha = file_sha256(pdf_path)
    pages_dir = paths.pages_dir(doc_id)
    stamp = _stamp_path(pages_dir)

    if not force and _stamp_matches(stamp, pdf_sha, dpi):
        n = sum(1 for _ in pages_dir.glob("p_*.png"))
        # A matching stamp with no pages behind it is a lie, so re-render
        # rather than reporting a cache hit that later stages cannot use.
        if n > 0:
            return RenderResult(doc_id=doc_id, n_pages=n, cached=True, dpi=dpi)

    n = render_pdf(pdf_path, pages_dir, dpi)
    _write_stamp(stamp, pdf_sha, dpi, n)
    return RenderResult(doc_id=doc_id, n_pages=n, cached=False, dpi=dpi)


# ---------------------------------------------------------------------------
# Standalone CLI:  python -m pipeline.extraction.render <doc_id> [--force]
# ---------------------------------------------------------------------------


def _main() -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Render one PDF to per-page PNGs.")
    p.add_argument("doc_id", help="doc_id with storage/raw/<doc_id>.pdf available.")
    p.add_argument("--force", action="store_true", help="Re-render even if stamp matches.")
    args = p.parse_args()

    cfg = Config.load()
    try:
        r = run(cfg, args.doc_id, force=args.force)
    except FileNotFoundError as e:
        sys.stderr.write(f"[FAIL] {e}\n")
        return 2

    print(
        f"[OK] doc_id={args.doc_id}  pages={r.n_pages}  dpi={r.dpi}  "
        f"{'cached' if r.cached else 'rendered'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
