"""make_diagrams.py — the documentation diagrams, as source rather than binaries.

    python docs/diagrams/make_diagrams.py

Writes a light and a dark variant of each figure. Markdown embeds them with a
``<picture>`` element so a reader gets the one that matches their theme:

    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="...-dark.svg">
      <img alt="..." src="...-light.svg">
    </picture>

Why generated and not drawn by hand: two themes have to stay identical in
geometry and differ only in colour, and a figure that drifts from the thing it
describes is worse than no figure. Editing one function and re-running is the
only way that stays true.

Three figures, one claim each. If you cannot say the claim in a sentence, the
figure is not ready:

  evidence-chain   Every value stays tethered to the pixels it came from, and
                   abstaining leaves nothing to tether.
  domain-layers    A new document type is four files you write. The engine is
                   the same code either way.
  retrieval-modes  The question decides the machinery. Money is added up in
                   Python, never by a language model.

Colours track the application's own palette (web/src/styles.css), so amber
means evidence highlight here for the same reason it does in the PDF viewer.
Fonts are system stacks: an SVG embedded in Markdown renders with the reader's
fonts, so anything else would silently fall back on someone else's machine.
"""
from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent

SANS = ("ui-sans-serif,-apple-system,BlinkMacSystemFont,'Segoe UI',"
        "Roboto,'Helvetica Neue',sans-serif")
MONO = ("ui-monospace,'Cascadia Code','SF Mono',Menlo,Consolas,monospace")

LIGHT = {
    "ink": "#1b1c21", "muted": "#6c7280", "subtle": "#9aa0ab",
    "rule": "#d8dae0", "paper": "#ffffff", "paperline": "#dcdee4",
    "panel": "#f5f6f8", "panelrule": "#e2e4e9",
    "accent": "#3a6fd8", "amber": "#a96a05", "amberfill": "#ffd35a",
    "amberop": "0.55", "teal": "#0f7a63",
    "slab": "#1b1c21", "slabink": "#f2f3f6", "slabmuted": "#9aa0ab",
    "slabrule": "none",
}
DARK = {
    "ink": "#ecedf1", "muted": "#9da3ae", "subtle": "#6c7280",
    "rule": "#33353d", "paper": "#1c1d23", "paperline": "#33353d",
    "panel": "#1a1b21", "panelrule": "#2a2b32",
    "accent": "#76a3ff", "amber": "#f0b429", "amberfill": "#ffd35a",
    "amberop": "0.30", "teal": "#4dd4ac",
    "slab": "#0f1014", "slabink": "#ecedf1", "slabmuted": "#7d838e",
    "slabrule": "#33353d",
}


# --------------------------------------------------------------------------- #
# Tiny SVG helpers. Deliberately thin: the figures below read as layout, not as
# a framework, because the geometry is the argument and it should be visible.
# --------------------------------------------------------------------------- #
def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def text(x: float, y: float, s: str, *, size: float = 13, fill: str = "#000",
         weight: str = "400", anchor: str = "start", mono: bool = False,
         spacing: float | None = None, opacity: float | None = None) -> str:
    attrs = [f'x="{x}"', f'y="{y}"', f'font-family="{MONO if mono else SANS}"',
             f'font-size="{size}"', f'font-weight="{weight}"', f'fill="{fill}"']
    if anchor != "start":
        attrs.append(f'text-anchor="{anchor}"')
    if spacing:
        attrs.append(f'letter-spacing="{spacing}"')
    if opacity is not None:
        attrs.append(f'opacity="{opacity}"')
    return f'<text {" ".join(attrs)}>{esc(s)}</text>'


def rect(x: float, y: float, w: float, h: float, *, fill: str = "none",
         stroke: str = "none", rx: float = 0, sw: float = 1,
         opacity: float | None = None, dash: str | None = None) -> str:
    attrs = [f'x="{x}"', f'y="{y}"', f'width="{w}"', f'height="{h}"',
             f'rx="{rx}"', f'fill="{fill}"']
    if stroke != "none":
        attrs += [f'stroke="{stroke}"', f'stroke-width="{sw}"']
    if dash:
        attrs.append(f'stroke-dasharray="{dash}"')
    if opacity is not None:
        attrs.append(f'opacity="{opacity}"')
    return f'<rect {" ".join(attrs)}/>'


def line(x1: float, y1: float, x2: float, y2: float, *, stroke: str,
         sw: float = 1, dash: str | None = None,
         opacity: float | None = None) -> str:
    attrs = [f'x1="{x1}"', f'y1="{y1}"', f'x2="{x2}"', f'y2="{y2}"',
             f'stroke="{stroke}"', f'stroke-width="{sw}"']
    if dash:
        attrs.append(f'stroke-dasharray="{dash}"')
    if opacity is not None:
        attrs.append(f'opacity="{opacity}"')
    return f'<line {" ".join(attrs)}/>'


def tether(x1: float, y1: float, x2: float, y2: float, *, stroke: str,
           sw: float = 1.6, opacity: float = 1.0) -> str:
    """A bond, not a flow. No arrowhead on purpose: clicking the value opens the
    page and clicking the page finds the value, so direction would be a lie."""
    mx = (x1 + x2) / 2
    return (f'<path d="M {x1} {y1} C {mx} {y1}, {mx} {y2}, {x2} {y2}" '
            f'fill="none" stroke="{stroke}" stroke-width="{sw}" '
            f'opacity="{opacity}" stroke-linecap="round"/>')


def arrow(x1: float, y1: float, x2: float, y2: float, *, stroke: str,
          sw: float = 1.6, marker: str = "head") -> str:
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" '
            f'stroke-width="{sw}" marker-end="url(#{marker})"/>')


def dot(cx: float, cy: float, r: float, fill: str) -> str:
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}"/>'


def check(cx: float, cy: float, colour: str, r: float = 8) -> str:
    d = r * 0.46
    return (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{colour}"/>'
            f'<path d="M {cx - d} {cy} l {d * 0.75} {d * 0.8} l {d * 1.25} '
            f'{-d * 1.6}" fill="none" stroke="#fff" stroke-width="1.9" '
            f'stroke-linecap="round" stroke-linejoin="round"/>')


def svg(w: float, h: float, body: str, *, title: str, desc: str) -> str:
    defs = ('<defs>'
            '<marker id="head" viewBox="0 0 10 10" refX="9" refY="5" '
            'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
            '<path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"/>'
            '</marker></defs>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
            f'width="{w}" height="{h}" role="img" '
            f'aria-labelledby="t d">'
            f'<title id="t">{esc(title)}</title>'
            f'<desc id="d">{esc(desc)}</desc>'
            f'{defs}{body}</svg>\n')


# --------------------------------------------------------------------------- #
# 1. evidence-chain
# --------------------------------------------------------------------------- #
# Claim: every value stays tethered to the pixels it came from, and abstaining
# leaves nothing to tether.
#
# The third row is the one that earns the figure. "Not Stated" is drawn with no
# thread, because there is nothing on the page to point at. Anywhere else that
# row would be a blank cell and the reader would assume a bug.
# --------------------------------------------------------------------------- #
def evidence_chain(c: dict) -> str:
    W, H = 1180, 520
    px, py, pw, ph = 64, 52, 306, 428          # the page facsimile
    fx = 566                                   # where the field rows start
    o: list[str] = []

    o.append(rect(px, py, pw, ph, fill=c["paper"], stroke=c["rule"], rx=3))
    o.append(text(px + 20, py + 36, "SOFTWARE LICENCE AGREEMENT",
                  size=9.5, fill=c["muted"], weight="600", spacing=1.1))
    o.append(line(px + 20, py + 46, px + pw - 20, py + 46,
                  stroke=c["paperline"]))

    # Body text as bars. Two of them carry the values on the right.
    widths = [258, 240, 262, 198, 251, 233, 259, 176,
              254, 262, 236, 205, 258, 219]
    hl_rows = {4: 0, 11: 1}                    # bar index -> field row index
    hl_y: list[float] = []
    y = py + 74
    for i, w in enumerate(widths):
        if i in hl_rows:
            o.append(rect(px + 16, y - 8, pw - 32, 17, fill=c["amberfill"],
                          rx=2, opacity=c["amberop"]))
            o.append(rect(px + 20, y - 3, w, 6.5, fill=c["ink"], rx=3,
                          opacity=0.72))
            hl_y.append(y)
        else:
            o.append(rect(px + 20, y - 3, w, 6.5, fill=c["ink"], rx=3,
                          opacity=0.17))
        y += 24 if i not in hl_rows else 28
    o.append(text(px + pw / 2, py + ph - 18, "page 2 of 9",
                  size=9.5, fill=c["subtle"], anchor="middle", mono=True))

    rows = [
        ("LICENCE FEE", "SGD 61,500 per year",
         "“an annual licence fee of SGD 61,500”",
         "p.2  rect 118,540 → 402,556", "verified"),
        ("GOVERNING LAW", "Laws of Singapore",
         "“governed by the laws of Singapore”",
         "p.2  rect 118,806 → 389,822", "pending"),
        ("MINIMUM COMMITMENT", "Not Stated", None, None, "absent"),
    ]
    row_y = [116, 262, 408]

    for i, (label, value, snippet, ref, state) in enumerate(rows):
        ry = row_y[i]
        muted_row = state == "absent"
        o.append(line(fx, ry - 46, W - 44, ry - 46, stroke=c["panelrule"]))
        o.append(text(fx + 26, ry - 24, label, size=9.5, fill=c["muted"],
                      weight="600", spacing=1.3))
        o.append(text(fx + 26, ry + 8, value, size=25,
                      fill=c["subtle"] if muted_row else c["ink"],
                      weight="600" if not muted_row else "400"))
        if snippet:
            o.append(text(fx + 26, ry + 36, snippet, size=12.5,
                          fill=c["muted"], mono=True))
            o.append(text(fx + 26, ry + 58, ref, size=10.5, fill=c["subtle"],
                          mono=True))
        else:
            o.append(text(fx + 26, ry + 36,
                          "no clause says otherwise, so nothing is anchored",
                          size=12.5, fill=c["subtle"]))

        if state == "verified":
            o.append(check(fx + 8, ry - 28, c["teal"], r=7.5))
            o.append(text(W - 44, ry - 24, "verified by 2 reviewers",
                          size=10.5, fill=c["teal"], anchor="end",
                          weight="600", spacing=0.4))
        elif state == "pending":
            o.append(dot(fx + 8, ry - 28, 5.5, c["amber"]))
            o.append(text(W - 44, ry - 24, "awaiting review", size=10.5,
                          fill=c["amber"], anchor="end", weight="600",
                          spacing=0.4))
        else:
            o.append(f'<circle cx="{fx + 8}" cy="{ry - 28}" r="5.5" '
                     f'fill="none" stroke="{c["subtle"]}" stroke-width="1.4"/>')
            o.append(text(W - 44, ry - 24, "abstained", size=10.5,
                          fill=c["subtle"], anchor="end", weight="600",
                          spacing=0.4))

    for i, y_hl in enumerate(hl_y):
        o.append(tether(px + pw + 6, y_hl, fx - 10, row_y[i] - 2,
                        stroke=c["amber"], opacity=0.85))
        o.append(dot(px + pw + 6, y_hl, 3.4, c["amber"]))
        o.append(dot(fx - 10, row_y[i] - 2, 3.4, c["amber"]))

    return svg(W, H, "".join(o),
               title="Values tethered to the page they came from",
               desc="A contract page with two highlighted clauses. Curved "
                    "threads join each highlight to a named field holding the "
                    "value, the verbatim snippet and the page rectangle. A "
                    "third field reads Not Stated and has no thread, because "
                    "there is nothing on the page to anchor it to.")


# --------------------------------------------------------------------------- #
# 2. domain-layers
# --------------------------------------------------------------------------- #
# Claim: a new document type is four files you write. The engine is the same
# code either way.
#
# The argument is the asymmetry, so the geometry is asymmetric: what you author
# is four separate, legible cards, and what you inherit is one undifferentiated
# slab you are not invited to open.
# --------------------------------------------------------------------------- #
def domain_layers(c: dict) -> str:
    W, H = 1180, 528
    o: list[str] = []

    o.append(text(64, 44, "YOU WRITE", size=10, fill=c["accent"],
                  weight="700", spacing=1.6))
    o.append(text(64, 66, "Four files. No Python.", size=15, fill=c["muted"]))

    cards = [
        ("configs/analyzers/<domain>/", "What kinds of document exist,",
         "and how the reader should treat", "each of them"),
        ("configs/ontology/<domain>.yaml", "What may exist in the graph:",
         "node labels, edge types, the", "vocabulary of party roles"),
        ("configs/packs/<domain>.yaml", "The build contract. A closed set,",
         "so anything unmapped is", "quarantined instead of guessed"),
        ("configs/views/<domain>_ops.yaml", "The field schema. What to capture,",
         "its type, and how its evidence", "is found. This is the target"),
    ]
    cw, gap = 258, 22
    cx0, cy, ch = 64, 92, 192
    for i, (name, l1, l2, l3) in enumerate(cards):
        x = cx0 + i * (cw + gap)
        o.append(rect(x, cy, cw, ch, fill=c["panel"], stroke=c["panelrule"],
                      rx=6))
        o.append(rect(x + 6, cy, cw - 12, 3.5, fill=c["accent"], rx=0))
        o.append(text(x + 20, cy + 38, name.split("/")[1].upper(), size=9.5,
                      fill=c["accent"], weight="700", spacing=1.4))
        o.append(text(x + 20, cy + 66, name, size=11.5, fill=c["ink"],
                      mono=True, weight="600"))
        o.append(line(x + 20, cy + 84, x + cw - 20, cy + 84,
                      stroke=c["panelrule"]))
        for j, ln in enumerate((l1, l2, l3)):
            o.append(text(x + 20, cy + 110 + j * 21, ln, size=12.5,
                          fill=c["muted"]))
        if i == 3:
            o.append(text(x + 20, cy + ch - 20,
                          "start here", size=11, fill=c["teal"],
                          weight="700", spacing=0.6))

    # The line between configuration and code.
    ly = cy + ch + 52
    o.append(line(64, ly, W - 64, ly, stroke=c["rule"], dash="5 5"))
    o.append(rect(W / 2 - 168, ly - 13, 336, 26, fill=c["panel"], rx=13))
    o.append(text(W / 2, ly + 5, "the line between configuration and code",
                  size=12, fill=c["muted"], anchor="middle"))

    # The slab.
    sy, sh = ly + 40, 118
    o.append(rect(64, sy, W - 128, sh, fill=c["slab"], rx=6,
                  stroke=c["slabrule"]))
    o.append(text(88, sy + 36, "YOU DO NOT TOUCH", size=10, fill=c["slabmuted"],
                  weight="700", spacing=1.6))
    o.append(text(88, sy + 62, "The engine. Identical for every domain.",
                  size=15, fill=c["slabink"]))
    chips = ["pipeline/extraction", "pipeline/kb", "pipeline/store", "api/rag",
             "api/routes", "web/src"]
    x = 88
    for ch_label in chips:
        w = 13 + len(ch_label) * 6.6
        o.append(rect(x, sy + 78, w, 24, fill="none", stroke=c["slabmuted"],
                      rx=12, opacity=0.45))
        o.append(text(x + w / 2, sy + 94, ch_label, size=10.5,
                      fill=c["slabmuted"], anchor="middle", mono=True))
        x += w + 9
    o.append(text(W - 88, sy + 62,
                  "one deployment serves one domain", size=12.5,
                  fill=c["slabmuted"], anchor="end"))

    return svg(W, H, "".join(o),
               title="The domain is four files, the engine is unchanged",
               desc="Four configuration file cards across the top, labelled "
                    "you write. Below a dashed line marked the line between "
                    "configuration and code, a single dark slab labelled the "
                    "engine, identical for every domain, listing the module "
                    "names a domain author never edits.")


# --------------------------------------------------------------------------- #
# 3. retrieval-modes
# --------------------------------------------------------------------------- #
# Claim: the question decides the machinery, and money is added up in Python.
#
# The two tracks are drawn in different grammars on purpose. Search is soft and
# curved and lands on one clause. Aggregation is a hard grid that lands on a
# ruled total. If both branches looked alike the figure would be saying the
# opposite of what the system does.
# --------------------------------------------------------------------------- #
def retrieval_modes(c: dict) -> str:
    W, H = 1180, 540
    o: list[str] = []
    midx = W / 2

    o.append(rect(midx - 300, 32, 600, 52, fill=c["panel"],
                  stroke=c["panelrule"], rx=26))
    o.append(text(midx, 64, "a question arrives", size=17, fill=c["ink"],
                  anchor="middle", weight="600"))

    o.append(f'<path d="M {midx - 40} 84 C {midx - 40} 128, 300 116, 300 152" '
             f'fill="none" stroke="{c["accent"]}" stroke-width="1.6" '
             f'marker-end="url(#head)"/>')
    o.append(f'<path d="M {midx + 40} 84 C {midx + 40} 128, 880 116, 880 152" '
             f'fill="none" stroke="{c["accent"]}" stroke-width="1.6" '
             f'marker-end="url(#head)"/>')

    # --- left: one clause answers it ------------------------------------- #
    o.append(text(300, 178, "THE ANSWER IS IN ONE CLAUSE", size=10,
                  fill=c["muted"], weight="700", spacing=1.4, anchor="middle"))
    o.append(text(300, 204, "“What is the licence fee?”", size=15,
                  fill=c["ink"], anchor="middle", mono=True))
    o.append(text(300, 232, "semantic search over the verified text",
                  size=12.5, fill=c["muted"], anchor="middle"))

    # Candidates narrowing to one. Soft edges, decreasing width, fading out:
    # search ranks, it does not enumerate.
    for i, (w, op) in enumerate(((296, 0.16), (238, 0.26), (182, 0.40))):
        o.append(rect(300 - w / 2, 258 + i * 15, w, 10, fill=c["ink"], rx=5,
                      opacity=op))
    o.append(rect(150, 312, 300, 66, fill=c["paper"], stroke=c["panelrule"],
                  rx=4))
    o.append(rect(160, 322, 280, 46, fill=c["amberfill"], rx=3,
                  opacity=c["amberop"]))
    o.append(rect(172, 334, 248, 7, fill=c["ink"], rx=3.5, opacity=0.72))
    o.append(rect(172, 353, 196, 7, fill=c["ink"], rx=3.5, opacity=0.72))
    o.append(text(300, 402, "one clause, cited and highlighted", size=12.5,
                  fill=c["amber"], anchor="middle", weight="600"))
    o.append(text(300, 424, "fails soft: a near miss is still recoverable",
                  size=11.5, fill=c["subtle"], anchor="middle"))

    # --- right: exhaustive and exact -------------------------------------- #
    o.append(text(880, 178, "THE ANSWER IS A CALCULATION", size=10,
                  fill=c["muted"], weight="700", spacing=1.4, anchor="middle"))
    o.append(text(880, 204, "“What do the fees total?”", size=15,
                  fill=c["ink"], anchor="middle", mono=True))
    o.append(text(880, 232, "deterministic query over the aligned fields",
                  size=12.5, fill=c["muted"], anchor="middle"))

    # A hard grid: every matching field, none skipped.
    gx, gy, gw, rh = 730, 258, 300, 25
    values = [("Northwind MSA", "48,000"), ("Fairhaven licence", "61,500"),
              ("Harbour Point SOW", "12,750")]
    o.append(rect(gx, gy, gw, rh * len(values), fill="none",
                  stroke=c["panelrule"], rx=3))
    for i, (who, amt) in enumerate(values):
        yy = gy + i * rh
        if i:
            o.append(line(gx, yy, gx + gw, yy, stroke=c["panelrule"]))
        o.append(text(gx + 14, yy + 17, who, size=12, fill=c["muted"]))
        o.append(text(gx + gw - 14, yy + 17, amt, size=12.5, fill=c["ink"],
                      anchor="end", mono=True))
    ty = gy + rh * len(values)
    o.append(line(gx, ty + 9, gx + gw, ty + 9, stroke=c["ink"], sw=1.6))
    o.append(text(gx + 14, ty + 32, "SGD, all three", size=12, fill=c["muted"]))
    o.append(text(gx + gw - 14, ty + 32, "122,250", size=15, fill=c["ink"],
                  anchor="end", mono=True, weight="700"))
    o.append(text(880, 402, "added in Python, never by the model", size=12.5,
                  fill=c["teal"], anchor="middle", weight="600"))
    o.append(text(880, 424, "exhaustive: a missed row is a wrong answer",
                  size=11.5, fill=c["subtle"], anchor="middle"))

    # --- what both branches owe you --------------------------------------- #
    o.append(line(64, 466, W - 64, 466, stroke=c["rule"], dash="5 5"))
    o.append(text(midx, 500,
                  "either way the answer names the clauses it used, and "
                  "“not in the documents” is one of the answers",
                  size=13, fill=c["muted"], anchor="middle"))

    return svg(W, H, "".join(o),
               title="Two retrieval modes, one per kind of question",
               desc="A question splits into two tracks drawn in different "
                    "styles. On the left, semantic search narrows soft "
                    "overlapping candidates to a single highlighted clause. On "
                    "the right, a deterministic query lists every matching "
                    "field in a ruled table and totals them below a rule, "
                    "computed in Python rather than by a language model.")


FIGURES = {
    "evidence-chain": evidence_chain,
    "domain-layers": domain_layers,
    "retrieval-modes": retrieval_modes,
}


def main() -> int:
    for name, fn in FIGURES.items():
        for theme, palette in (("light", LIGHT), ("dark", DARK)):
            path = OUT / f"{name}-{theme}.svg"
            path.write_text(fn(palette), encoding="utf-8")
            print(f"  {path.relative_to(OUT.parents[1])}  "
                  f"{path.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
