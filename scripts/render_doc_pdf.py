"""Render a paged docs HTML file to PDF via headless Edge, then rasterize every
page to PNG for visual QA.

Usage:
    python -m scripts.render_doc_pdf docs/product_workflow.html

Writes the PDF next to the HTML (same stem) and QA PNGs to %TEMP%/pdf_qa/<stem>/.
The QA PNGs are the point: open each one and look for overflow, overlap and
clipped text before calling the document done.
"""

import os
import pathlib
import subprocess
import sys
import tempfile

import pypdfium2 as pdfium

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    html = pathlib.Path(sys.argv[1]).resolve()
    if not html.exists():
        raise SystemExit(f"not found: {html}")
    pdf = html.with_suffix(".pdf")

    browser = EDGE if os.path.exists(EDGE) else CHROME
    subprocess.run(
        [
            browser,
            "--headless",
            "--disable-gpu",
            f"--print-to-pdf={pdf}",
            "--no-pdf-header-footer",
            "--force-device-scale-factor=1",
            html.as_uri(),
        ],
        check=True,
        timeout=180,
    )

    outdir = pathlib.Path(tempfile.gettempdir()) / "pdf_qa" / html.stem
    outdir.mkdir(parents=True, exist_ok=True)
    for old in outdir.glob("p*.png"):
        old.unlink()
    doc = pdfium.PdfDocument(pdf)
    for i in range(len(doc)):
        doc[i].render(scale=110 / 72.0).to_pil().convert("RGB").save(
            outdir / f"p{i + 1:02d}.png")
    print(f"{pdf.name}: {len(doc)} pages")
    print(f"QA PNGs: {outdir}")


if __name__ == "__main__":
    main()
