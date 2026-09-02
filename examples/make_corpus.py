"""make_corpus.py — regenerate the sample contract family in examples/corpus/.

    pip install reportlab
    python examples/make_corpus.py

You do not need to run this. The PDFs are committed, and running the sample
walkthrough only needs the PDFs. This script exists so the corpus is readable
and editable text rather than three opaque binaries, which matters because the
corpus is a test fixture: every expected answer in examples/README.md is a
claim about the wording below.

The three documents are a deliberate shape, not filler:

  * The base agreement sets every field once.
  * The first amendment changes two of them and says nothing about the rest.
  * The second amendment changes a third, deletes a clause outright, and also
    says nothing about the fee.

So "the current licence fee" is in the FIRST amendment, not the newest
document, and "the current initial term" is in the second. Any system that
answers by reading the newest document gets one of those two wrong. That is
the whole reason this corpus exists.

Fictional parties, fictional numbers, ordinary commercial boilerplate. Nothing
here is legal drafting and none of it should be used as such.
"""
from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

OUT = Path(__file__).resolve().parent / "corpus"


# --------------------------------------------------------------------------- #
# Type. A contract facsimile has to look set rather than typed, so a serif is
# doing real work here. DejaVu Serif is libre and safe to embed in a PDF that
# ships in an MIT repository. It travels with matplotlib, which is the most
# likely place to find it on a machine that has neither a font manager nor a
# system package. Falling back to a built-in keeps the script runnable, and
# says so, because a silent substitution changes what the committed PDFs look
# like next time somebody regenerates them.
# --------------------------------------------------------------------------- #
def register_serif() -> tuple[str, str]:
    candidates: list[Path] = []
    try:
        import matplotlib
        ttf = Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf"
        candidates.append(ttf)
    except ImportError:
        pass
    candidates += [Path("/usr/share/fonts/truetype/dejavu"),
                   Path("/usr/share/fonts/dejavu"),
                   Path("/Library/Fonts")]
    for d in candidates:
        regular, bold = d / "DejaVuSerif.ttf", d / "DejaVuSerif-Bold.ttf"
        if regular.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont("Body", str(regular)))
            pdfmetrics.registerFont(TTFont("Body-Bold", str(bold)))
            return "Body", "Body-Bold"
    print("  note: DejaVu Serif not found, falling back to a built-in face.",
          file=sys.stderr)
    return "Times-Roman", "Times-Bold"


BODY, BOLD = register_serif()

TITLE = ParagraphStyle("title", fontName=BOLD, fontSize=13, leading=17,
                       alignment=TA_CENTER, spaceAfter=4)
SUBTITLE = ParagraphStyle("subtitle", fontName=BODY, fontSize=9.5, leading=13,
                          alignment=TA_CENTER, spaceAfter=18)
HEAD = ParagraphStyle("head", fontName=BOLD, fontSize=10, leading=14,
                      spaceBefore=11, spaceAfter=4)
PARA = ParagraphStyle("para", fontName=BODY, fontSize=9.5, leading=14.5,
                      alignment=TA_JUSTIFY, spaceAfter=7)
RECITAL = ParagraphStyle("recital", fontName=BODY, fontSize=9.5, leading=14.5,
                         alignment=TA_JUSTIFY, spaceAfter=7,
                         leftIndent=0.7 * cm, rightIndent=0.7 * cm)
SIGLABEL = ParagraphStyle("siglabel", fontName=BODY, fontSize=8.5, leading=12)
SIGNAME = ParagraphStyle("signame", fontName=BOLD, fontSize=9.5, leading=13)


def signatures(rows: list[tuple[str, str, str]]) -> KeepTogether:
    """The execution page. Two columns of ruled lines, because the party and
    signatory fields are read from here as often as from the recitals."""
    cells = []
    for org, name, title in rows:
        cells.append([Paragraph(f"For and on behalf of<br/><b>{org}</b>",
                                SIGLABEL)])
        cells.append([Paragraph("&nbsp;", SIGLABEL)])
        cells.append([Paragraph(name, SIGNAME)])
        cells.append([Paragraph(title, SIGLABEL)])
    left, right = cells[:4], cells[4:]
    table = Table([[a[0], b[0]] for a, b in zip(left, right)],
                  colWidths=[7.6 * cm, 7.6 * cm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
        ("LINEBELOW", (0, 1), (-1, 1), 0.6, "#000000"),
    ]))
    return KeepTogether([Spacer(1, 16), table])


# --------------------------------------------------------------------------- #
# The documents.
# --------------------------------------------------------------------------- #
LICENSOR = "Northwind Logistics Pte. Ltd."
LICENSEE = "Fairhaven Systems Limited"

BASE = [
    ("title", "MASTER SOFTWARE LICENSE AGREEMENT"),
    ("subtitle", "Dated 14 March 2023"),
    ("recital", f"THIS AGREEMENT is made on 14 March 2023 BETWEEN "
                f"<b>{LICENSOR}</b>, a company incorporated in Singapore with "
                f"registered office at 12 Kallang Avenue, Singapore 339511 "
                f"(the “Licensor” and the “Disclosing "
                f"Party”), and <b>{LICENSEE}</b>, a company incorporated "
                f"in Singapore with registered office at 400 Harbour Point "
                f"Road, Singapore 099254 (the “Licensee” and the "
                f"“Receiving Party”)."),
    ("head", "1. Definitions"),
    ("para", "“Effective Date” means 1 April 2023. “Software” "
             "means the fleet scheduling platform identified in Schedule 1, "
             "together with any updates the Licensor makes generally available. "
             "“Confidential Information” means information disclosed "
             "by one party to the other that is marked confidential or that a "
             "reasonable person would understand to be confidential."),
    ("head", "2. Grant of License"),
    ("para", "The Licensor grants to the Licensee a non-exclusive, "
             "non-transferable license to use the Software for its internal "
             "business operations during the Term, subject to payment of the "
             "fees set out in Clause 3."),
    ("head", "3. License Fee"),
    ("para", "The Licensee shall pay to the Licensor an annual license fee of "
             "<b>SGD 48,000</b>, payable in advance within thirty (30) days of "
             "the Effective Date and on each anniversary of it. All fees are "
             "exclusive of goods and services tax."),
    ("head", "4. Minimum Commitment"),
    ("para", "The Licensee commits to a minimum aggregate spend of "
             "<b>SGD 120,000</b> across the Initial Term. Any shortfall against "
             "that commitment falls due on the last day of the Initial Term."),
    ("head", "5. Term and Renewal"),
    ("para", "This Agreement commences on the Effective Date and shall continue "
             "for an initial term of <b>three (3) years</b> (the “Initial "
             "Term”). On expiry of the Initial Term this Agreement shall "
             "automatically renew for successive renewal terms of "
             "<b>twelve (12) months</b> each, unless either party gives written "
             "notice of non-renewal at least sixty (60) days before the end of "
             "the then-current term."),
    ("head", "6. Confidentiality"),
    ("para", "The Receiving Party shall maintain in confidence all Confidential "
             "Information of the Disclosing Party, shall use it only for the "
             "purposes of this Agreement, and shall not disclose it to any third "
             "party without prior written consent. This obligation survives "
             "termination or expiry of this Agreement for a period of "
             "<b>five (5) years</b>."),
    ("head", "7. Records and Audit"),
    ("para", "The Licensor may, on not less than thirty (30) days’ written "
             "notice and no more than once in any twelve month period, audit the "
             "books and records of the Licensee to the extent reasonably "
             "necessary to verify compliance with this Agreement. Any audit "
             "shall be conducted during normal business hours."),
    ("head", "8. Termination for Convenience"),
    ("para", "Either party may terminate this Agreement for convenience, without "
             "cause, upon <b>ninety (90) days’</b> prior written notice to "
             "the other party. Fees paid in respect of any period after the "
             "effective date of such termination shall be refunded on a pro rata "
             "basis."),
    ("head", "9. Limitation of Liability"),
    ("para", "Save in respect of fraud, death or personal injury, the aggregate "
             "liability of either party arising out of or in connection with "
             "this Agreement shall not exceed <b>SGD 250,000</b>. Neither party "
             "shall be liable for indirect or consequential loss."),
    ("head", "10. Governing Law and Jurisdiction"),
    ("para", "This Agreement and any dispute arising out of it shall be governed "
             "by and construed in accordance with <b>the laws of Singapore</b>. "
             "The parties irrevocably agree that the <b>courts of Singapore</b> "
             "shall have exclusive jurisdiction to settle any such dispute."),
    ("head", "11. Entire Agreement"),
    ("para", "This Agreement, together with its Schedules, constitutes the entire "
             "agreement between the parties in relation to its subject matter and "
             "supersedes all prior discussions and understandings."),
    ("sig", [(LICENSOR, "Amara Okonjo", "Director"),
             (LICENSEE, "Priya Raman", "Chief Operating Officer")]),
]

FIRST = [
    ("title", "FIRST AMENDMENT TO THE MASTER SOFTWARE LICENSE AGREEMENT"),
    ("subtitle", "Dated 2 September 2024"),
    ("recital", f"THIS FIRST AMENDMENT (this “Amendment”) is made on "
                f"2 September 2024 to the <b>Master Software License Agreement "
                f"dated 14 March 2023</b> between <b>{LICENSOR}</b> and "
                f"<b>{LICENSEE}</b> (the “Agreement”). Terms defined "
                f"in the Agreement have the same meaning in this Amendment."),
    ("recital", "WHEREAS the parties wish to vary the license fee and the "
                "limitation of liability, and to leave the remainder of the "
                "Agreement unchanged, the parties agree as follows."),
    ("head", "1. Amendment to Clause 3 (License Fee)"),
    ("para", "Clause 3 of the Agreement is deleted in its entirety and replaced "
             "with the following: “The Licensee shall pay to the Licensor "
             "an annual license fee of <b>SGD 61,500</b>, payable in advance on "
             "each anniversary of the Effective Date. All fees are exclusive of "
             "goods and services tax.” The revised fee applies from the "
             "anniversary falling on 1 April 2025."),
    ("head", "2. Amendment to Clause 9 (Limitation of Liability)"),
    ("para", "In Clause 9 of the Agreement, the figure “SGD 250,000” is "
             "deleted and replaced with <b>“SGD 400,000”</b>."),
    ("head", "3. No Other Changes"),
    ("para", "Save as expressly amended by this Amendment, all other terms and "
             "conditions of the Agreement remain in full force and effect and "
             "are unaffected. For the avoidance of doubt, the Initial Term, the "
             "minimum commitment, the audit right and the governing law of the "
             "Agreement are unchanged."),
    ("head", "4. Governing Law"),
    ("para", "This Amendment shall be governed by and construed in accordance "
             "with the laws of Singapore."),
    ("sig", [(LICENSOR, "Amara Okonjo", "Director"),
             (LICENSEE, "Priya Raman", "Chief Operating Officer")]),
]

SECOND = [
    ("title", "SECOND AMENDMENT TO THE MASTER SOFTWARE LICENSE AGREEMENT"),
    ("subtitle", "Dated 20 January 2025"),
    ("recital", f"THIS SECOND AMENDMENT (this “Amendment”) is made on "
                f"20 January 2025 to the <b>Master Software License Agreement "
                f"dated 14 March 2023</b> between <b>{LICENSOR}</b> and "
                f"<b>{LICENSEE}</b>, <b>as amended by the First Amendment dated "
                f"2 September 2024</b> (together, the “Agreement”)."),
    ("recital", "WHEREAS the parties wish to extend the Initial Term and to "
                "remove the right to terminate for convenience, the parties "
                "agree as follows."),
    ("head", "1. Amendment to Clause 5 (Term and Renewal)"),
    ("para", "In Clause 5 of the Agreement, the words “three (3) years” "
             "are deleted and replaced with <b>“five (5) years”</b>. "
             "The Initial Term therefore expires on 31 March 2028. The renewal "
             "mechanism in Clause 5 is otherwise unchanged."),
    ("head", "2. Deletion of Clause 8 (Termination for Convenience)"),
    ("para", "Clause 8 of the Agreement is <b>deleted in its entirety</b>. With "
             "effect from the date of this Amendment neither party may terminate "
             "the Agreement for convenience, and termination is available only "
             "for material breach or insolvency."),
    ("head", "3. No Other Changes"),
    ("para", "Save as expressly amended by this Amendment, all other terms and "
             "conditions of the Agreement, as previously amended, remain in full "
             "force and effect and are unaffected."),
    ("head", "4. Governing Law"),
    ("para", "This Amendment shall be governed by and construed in accordance "
             "with the laws of Singapore."),
    ("sig", [(LICENSOR, "Amara Okonjo", "Director"),
             (LICENSEE, "Devansh Mehta", "Chief Financial Officer")]),
]

DOCS = {
    "01-master-license-agreement-2023.pdf": BASE,
    "02-first-amendment-2024.pdf": FIRST,
    "03-second-amendment-2025.pdf": SECOND,
}

STYLES = {"title": TITLE, "subtitle": SUBTITLE, "head": HEAD,
          "para": PARA, "recital": RECITAL}


def footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont(BODY, 7.5)
    canvas.setFillGray(0.45)
    canvas.drawCentredString(A4[0] / 2, 1.4 * cm, f"Page {doc.page}")
    canvas.restoreState()


def build(path: Path, blocks: list) -> None:
    doc = SimpleDocTemplate(
        str(path), pagesize=A4, topMargin=2.4 * cm, bottomMargin=2.4 * cm,
        leftMargin=2.6 * cm, rightMargin=2.6 * cm,
        title=path.stem, author="Sample corpus")
    story: list = []
    for kind, payload in blocks:
        if kind == "sig":
            story.append(signatures(payload))
        elif kind == "pagebreak":
            story.append(PageBreak())
        else:
            story.append(Paragraph(payload, STYLES[kind]))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, blocks in DOCS.items():
        path = OUT / name
        build(path, blocks)
        print(f"  {path.relative_to(OUT.parents[1])}  "
              f"{path.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
