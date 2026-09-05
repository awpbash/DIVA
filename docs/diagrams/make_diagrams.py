"""make_diagrams.py: the documentation diagrams, as source rather than binaries.

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

Six figures, one claim each. If you cannot say the claim in a sentence, the
figure is not ready:

  evidence-chain   Every value stays tethered to the pixels it came from, and
                   abstaining leaves nothing to tether.
  domain-layers    A new document type is four files you write. The engine is
                   the same code either way.
  retrieval-modes  The question decides the machinery. Money is added up in
                   Python, never by a language model.
  architecture     The whole application is one container, talking to exactly
                   three things outside itself.
  workflow         The five steps from a PDF to a cited answer.
  scale            The same per-field walk runs whether there is one chain or
                   six, and a real chain caught a bug the synthetic one never
                   could.

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


# --------------------------------------------------------------------------- #
# System icons, used only by `architecture` below. A folder for the local
# filesystem stays hand-drawn, flat, single-stroke, like every other figure
# here, because "a filesystem" is a role, not a product. The container
# runtime, the model endpoint, and the database service are not roles, they
# are specific products, so `brand_logo` below embeds each one's own real
# mark instead of an invented stand-in.
# --------------------------------------------------------------------------- #
def folder_icon(x: float, y: float, w: float, h: float, *, fill: str,
                 back: str, stroke: str, sw: float = 1.6) -> str:
    tabw = w * 0.42
    return (rect(x, y, tabw, h * 0.22, fill=back, rx=3) +
            rect(x, y + h * 0.14, w, h * 0.86, fill=fill, stroke=stroke, rx=6, sw=sw))


def brand_logo(cx: float, cy: float, size: float, inner: str, *,
               view_w: float, view_h: float) -> str:
    """Centre a real product mark's own path data at (cx, cy).

    `inner` is that product's official SVG markup (defs and paths, sourced
    from the product itself, not redrawn), copied verbatim rather than
    approximated, so the mark stays recognisable and stays correct. `size`
    is the rendered width or height, whichever the source is wider or taller
    in, so different marks (a square glyph, a taller triangular one) come out
    visually comparable instead of all forced into one bounding box.
    """
    k = size / max(view_w, view_h)
    tx, ty = cx - view_w * k / 2, cy - view_h * k / 2
    return f'<g transform="translate({tx} {ty}) scale({k})">{inner}</g>'


# Official marks, embedded verbatim. OpenAI's and Microsoft's were both
# pulled from the simple-icons library the rest of this project's badges
# read from (Microsoft over a trademark dispute that took its whole icon
# set with it), so these two are sourced from Iconify's separately
# maintained `logos` collection instead, see the "badges/" section of
# docs/images/README.md for the same problem on the Markdown side.
LOGO_OPENAI = (
    '<path fill="white" d="M239.184 106.203a64.72 64.72 0 0 0-5.576-53.103C219.452 28.459 191 15.784 163.213 21.74A65.586 65.586 0 0 0 52.096 45.22a64.72 64.72 0 0 0-43.23 31.36c-14.31 24.602-11.061 55.634 8.033 76.74a64.67 64.67 0 0 0 5.525 53.102c14.174 24.65 42.644 37.324 70.446 31.36a64.72 64.72 0 0 0 48.754 21.744c28.481.025 53.714-18.361 62.414-45.481a64.77 64.77 0 0 0 43.229-31.36c14.137-24.558 10.875-55.423-8.083-76.483m-97.56 136.338a48.4 48.4 0 0 1-31.105-11.255l1.535-.87l51.67-29.825a8.6 8.6 0 0 0 4.247-7.367v-72.85l21.845 12.636c.218.111.37.32.409.563v60.367c-.056 26.818-21.783 48.545-48.601 48.601M37.158 197.93a48.35 48.35 0 0 1-5.781-32.589l1.534.921l51.722 29.826a8.34 8.34 0 0 0 8.441 0l63.181-36.425v25.221a.87.87 0 0 1-.358.665l-52.335 30.184c-23.257 13.398-52.97 5.431-66.404-17.803M23.549 85.38a48.5 48.5 0 0 1 25.58-21.333v61.39a8.29 8.29 0 0 0 4.195 7.316l62.874 36.272l-21.845 12.636a.82.82 0 0 1-.767 0L41.353 151.53c-23.211-13.454-31.171-43.144-17.804-66.405zm179.466 41.695l-63.08-36.63L161.73 77.86a.82.82 0 0 1 .768 0l52.233 30.184a48.6 48.6 0 0 1-7.316 87.635v-61.391a8.54 8.54 0 0 0-4.4-7.213m21.742-32.69l-1.535-.922l-51.619-30.081a8.39 8.39 0 0 0-8.492 0L99.98 99.808V74.587a.72.72 0 0 1 .307-.665l52.233-30.133a48.652 48.652 0 0 1 72.236 50.391zM88.061 139.097l-21.845-12.585a.87.87 0 0 1-.41-.614V65.685a48.652 48.652 0 0 1 79.757-37.346l-1.535.87l-51.67 29.825a8.6 8.6 0 0 0-4.246 7.367zm11.868-25.58L128.067 97.3l28.188 16.218v32.434l-28.086 16.218l-28.188-16.218z"/>'
)
LOGO_OPENAI_VB = (256, 260)

LOGO_AZURE = (
    '<defs><linearGradient id="dgAzureA" x1="58.972%" x2="37.191%" y1="7.411%" y2="103.762%"><stop offset="0%" stop-color="#114a8b"/><stop offset="100%" stop-color="#0669bc"/></linearGradient><linearGradient id="dgAzureB" x1="59.719%" x2="52.691%" y1="52.313%" y2="54.864%"><stop offset="0%" stop-opacity=".3"/><stop offset="7.1%" stop-opacity=".2"/><stop offset="32.1%" stop-opacity=".1"/><stop offset="62.3%" stop-opacity=".05"/><stop offset="100%" stop-opacity="0"/></linearGradient><linearGradient id="dgAzureC" x1="37.279%" x2="62.473%" y1="4.6%" y2="99.979%"><stop offset="0%" stop-color="#3ccbf4"/><stop offset="100%" stop-color="#2892df"/></linearGradient></defs>'
    '<path fill="url(#dgAzureA)" d="M85.343.003h75.753L82.457 233a12.08 12.08 0 0 1-11.442 8.216H12.06A12.06 12.06 0 0 1 .633 225.303L73.898 8.219A12.08 12.08 0 0 1 85.343 0z"/>'
    '<path fill="#0078d4" d="M195.423 156.282H75.297a5.56 5.56 0 0 0-3.796 9.627l77.19 72.047a12.14 12.14 0 0 0 8.28 3.26h68.02z"/>'
    '<path fill="url(#dgAzureB)" d="M85.343.003a11.98 11.98 0 0 0-11.471 8.376L.723 225.105a12.045 12.045 0 0 0 11.37 16.112h60.475a12.93 12.93 0 0 0 9.921-8.437l14.588-42.991l52.105 48.6a12.33 12.33 0 0 0 7.757 2.828h67.766l-29.721-84.935l-86.643.02L161.37.003z"/>'
    '<path fill="url(#dgAzureC)" d="M182.098 8.207A12.06 12.06 0 0 0 170.67.003H86.245c5.175 0 9.773 3.301 11.428 8.204L170.94 225.3a12.062 12.062 0 0 1-11.428 15.92h84.429a12.062 12.062 0 0 0 11.425-15.92z"/>'
)
LOGO_AZURE_VB = (256, 242)


def docker_logo(fill: str) -> tuple[str, tuple[float, float]]:
    """Docker's mark is a single flat path, so it recolours per theme like
    every hand-drawn icon here, unlike the multi-tone OpenAI/Azure marks
    above, which look wrong in anything but their own official colours."""
    d = ("M13.983 11.078h2.119a.186.186 0 00.186-.185V9.006a.186.186 0 00-.186-.186h-2.119a.185.185 0 00-.185.185v1.888c0 "
         ".102.083.185.185.185m-2.954-5.43h2.118a.186.186 0 00.186-.186V3.574a.186.186 0 00-.186-.185h-2.118a.185.185 0 "
         "00-.185.185v1.888c0 .102.082.185.185.185m0 2.716h2.118a.187.187 0 00.186-.186V6.29a.186.186 0 00-.186-.185h-2.118a"
         ".185.185 0 00-.185.185v1.887c0 .102.082.185.185.186m-2.93 0h2.12a.186.186 0 00.184-.186V6.29a.185.185 0 "
         "00-.185-.185H8.1a.185.185 0 00-.185.185v1.887c0 .102.083.185.185.186m-2.964 0h2.119a.186.186 0 00.185-.186V6.29a"
         ".185.185 0 00-.185-.185H5.136a.186.186 0 00-.186.185v1.887c0 .102.084.185.186.186m5.893 2.715h2.118a.186.186 0 "
         "00.186-.185V9.006a.186.186 0 00-.186-.186h-2.118a.185.185 0 00-.185.185v1.888c0 .102.082.185.185.185m-2.93 0h2.12a"
         ".185.185 0 00.184-.185V9.006a.185.185 0 00-.184-.186h-2.12a.185.185 0 00-.184.185v1.888c0 .102.083.185.185.185"
         "m-2.964 0h2.119a.185.185 0 00.185-.185V9.006a.185.185 0 00-.184-.186h-2.12a.186.186 0 00-.186.186v1.887c0 "
         ".102.084.185.186.185m-2.92 0h2.12a.185.185 0 00.184-.185V9.006a.185.185 0 00-.184-.186h-2.12a.185.185 0 "
         "00-.184.185v1.888c0 .102.082.185.185.185M23.763 9.89c-.065-.051-.672-.51-1.954-.51-.338.001-.676.03-1.01.087-"
         ".248-1.7-1.653-2.53-1.716-2.566l-.344-.199-.226.327c-.284.438-.49.922-.612 1.43-.23.97-.09 1.882.403 2.661-.595"
         ".332-1.55.413-1.744.42H.751a.751.751 0 00-.75.748 11.376 11.376 0 00.692 4.062c.545 1.428 1.355 2.48 2.41 3.124 "
         "1.18.723 3.1 1.137 5.275 1.137.983.003 1.963-.086 2.93-.266a12.248 12.248 0 003.823-1.389c.98-.567 1.86-1.288 "
         "2.61-2.136 1.252-1.418 1.998-2.997 2.553-4.4h.221c1.372 0 2.215-.549 2.68-1.009.309-.293.55-.65.707-1.046l.098-.288Z")
    return f'<path fill="{fill}" d="{d}"/>', (24, 24)


def page_icon(x: float, y: float, s: float, *, fill: str, stroke: str) -> str:
    """A small document icon for the reader-facing workflow figure."""
    w, h = 54 * s, 70 * s
    fold = 16 * s
    body = (f"M {x} {y} H {x+w-fold} L {x+w} {y+fold} V {y+h} "
            f"H {x} Z")
    fold_path = f"M {x+w-fold} {y} V {y+fold} H {x+w}"
    return (f'<path d="{body}" fill="{fill}" stroke="{stroke}" stroke-width="1.8" '
            f'stroke-linejoin="round"/>'
            f'<path d="{fold_path}" fill="none" stroke="{stroke}" stroke-width="1.8" '
            f'stroke-linejoin="round"/>'
            + line(x + 13*s, y + 38*s, x + 41*s, y + 38*s, stroke=stroke, sw=1.8)
            + line(x + 13*s, y + 49*s, x + 36*s, y + 49*s, stroke=stroke, sw=1.8)
            + line(x + 13*s, y + 60*s, x + 41*s, y + 60*s, stroke=stroke, sw=1.8))


def scan_icon(x: float, y: float, s: float, *, fill: str, stroke: str,
              accent: str) -> str:
    """A scanner/text-box icon: OCR text plus the geometry it supplies."""
    w, h = 72 * s, 60 * s
    parts = [rect(x, y + 10*s, w, h - 10*s, fill=fill, stroke=stroke, rx=7, sw=1.8)]
    parts += [line(x + 13*s, y + 26*s, x + 45*s, y + 26*s, stroke=stroke, sw=1.7),
              line(x + 13*s, y + 37*s, x + 53*s, y + 37*s, stroke=stroke, sw=1.7),
              line(x + 13*s, y + 48*s, x + 39*s, y + 48*s, stroke=stroke, sw=1.7)]
    parts += [rect(x + 49*s, y + 20*s, 13*s, 22*s, fill="none", stroke=accent,
                   rx=2, sw=1.8),
              line(x + 7*s, y + 10*s, x + 17*s, y, stroke=accent, sw=1.8),
              line(x + 55*s, y, x + 65*s, y + 10*s, stroke=accent, sw=1.8)]
    return "".join(parts)


def field_icon(x: float, y: float, s: float, *, fill: str, stroke: str,
               accent: str) -> str:
    """A field card with a highlighted value and a verification tick."""
    w, h = 72 * s, 62 * s
    parts = [rect(x, y, w, h, fill=fill, stroke=stroke, rx=7, sw=1.8),
             line(x + 13*s, y + 18*s, x + 47*s, y + 18*s, stroke=stroke, sw=1.7),
             rect(x + 13*s, y + 28*s, 39*s, 10*s, fill=accent, rx=3, opacity=0.8),
             line(x + 13*s, y + 48*s, x + 36*s, y + 48*s, stroke=stroke, sw=1.7),
             circle_icon(x + 58*s, y + 48*s, 7*s, fill=accent, stroke=accent)]
    parts.append(f'<path d="M {x+54*s} {y+48*s} l {3*s} {3*s} l {6*s} {-7*s}" '
                 f'fill="none" stroke="{fill}" stroke-width="1.6" '
                 f'stroke-linecap="round" stroke-linejoin="round"/>')
    return "".join(parts)


def circle_icon(cx: float, cy: float, r: float, *, fill: str, stroke: str) -> str:
    return (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="1.2"/>')


def graph_icon(cx: float, cy: float, s: float, *, fill: str, stroke: str,
               accent: str) -> str:
    """Three linked records, kept deliberately simpler than a graph screenshot."""
    pts = [(cx - 29*s, cy + 17*s), (cx, cy - 22*s), (cx + 29*s, cy + 17*s)]
    parts = [line(pts[0][0], pts[0][1], pts[1][0], pts[1][1], stroke=stroke, sw=1.8),
             line(pts[1][0], pts[1][1], pts[2][0], pts[2][1], stroke=stroke, sw=1.8),
             line(pts[0][0], pts[0][1], pts[2][0], pts[2][1], stroke=accent, sw=1.8)]
    for i, (px, py) in enumerate(pts):
        parts.append(circle_icon(px, py, 10*s, fill=accent if i == 1 else fill,
                                 stroke=accent if i == 1 else stroke))
    return "".join(parts)


def chat_icon(x: float, y: float, s: float, *, fill: str, stroke: str,
              accent: str) -> str:
    """A chat bubble with a citation chip."""
    w, h = 78*s, 58*s
    bubble = (f"M {x+8*s} {y} H {x+w-8*s} Q {x+w} {y} {x+w} {y+8*s} "
              f"V {y+h-16*s} Q {x+w} {y+h-8*s} {x+w-8*s} {y+h-8*s} "
              f"H {x+31*s} L {x+18*s} {y+h+5*s} V {y+h-8*s} H {x+8*s} "
              f"Q {x} {y+h-8*s} {x} {y+h-16*s} V {y+8*s} Q {x} {y} {x+8*s} {y} Z")
    return (f'<path d="{bubble}" fill="{fill}" stroke="{stroke}" stroke-width="1.8" '
            f'stroke-linejoin="round"/>'
            + line(x + 15*s, y + 22*s, x + 58*s, y + 22*s, stroke=stroke, sw=1.7)
            + line(x + 15*s, y + 34*s, x + 43*s, y + 34*s, stroke=stroke, sw=1.7)
            + rect(x + 47*s, y + 39*s, 20*s, 9*s, fill=accent, rx=4))


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


# --------------------------------------------------------------------------- #
# 4. architecture
# --------------------------------------------------------------------------- #
# Claim: the whole application is one container. A browser is the one thing
# it answers to. A model endpoint, a database, and a local storage folder are
# the three things it depends on behind that.
#
# The one detail worth the reader's attention is what does NOT get its own
# box: the three things inside the container (API, retrieval agent, ingestion
# pipeline) live in the same process and the same deploy, which is the point
# of drawing them nested rather than as peers of the browser or the database.
# --------------------------------------------------------------------------- #
def architecture(c: dict) -> str:
    W, H = 1180, 560
    o: list[str] = []

    bx, by, bw, bh = 56, 210, 220, 160
    o.append(rect(bx, by, bw, bh, fill=c["paper"], stroke=c["rule"], rx=8))
    o.append(rect(bx, by, bw, 24, fill=c["panel"], stroke=c["rule"], rx=8))
    o.append(rect(bx, by + 14, bw, 10, fill=c["panel"]))
    for i in range(3):
        o.append(dot(bx + 16 + i * 14, by + 12, 3.2, c["subtle"]))
    o.append(text(bx + bw / 2, by + 66, "React workspace", size=15, fill=c["ink"],
                  anchor="middle", weight="700"))
    o.append(text(bx + bw / 2, by + 92, "chat · review · knowledge", size=11.5,
                  fill=c["muted"], anchor="middle"))
    o.append(text(bx + bw / 2, by + 110, "explore · admin", size=11.5,
                  fill=c["muted"], anchor="middle"))
    o.append(text(bx + bw / 2, by + bh - 16, "Browser", size=10, fill=c["subtle"],
                  anchor="middle", weight="700", spacing=1.4))

    kx, ky, kw, kh = 330, 60, 420, 440
    o.append(rect(kx, ky, kw, kh, fill="none", stroke=c["rule"], rx=12, sw=1.6,
                  dash="6 5"))
    docker_inner, docker_vb = docker_logo(c["accent"])
    o.append(brand_logo(kx + 28, ky + 26, 22, docker_inner,
                        view_w=docker_vb[0], view_h=docker_vb[1]))
    o.append(text(kx + 54, ky + 34, "APPLICATION CONTAINER", size=10,
                  fill=c["accent"], weight="700", spacing=1.4))
    o.append(text(kx + kw - 18, ky + 34, "one image, one port", size=11,
                  fill=c["subtle"], anchor="end"))

    cards = [
        ("FastAPI", "routes · auth · RBAC"),
        ("Retrieval agent", "planner → tools → synthesis"),
        ("Ingestion pipeline", "read → extract → build"),
    ]
    cy0, ch, gap = ky + 58, 96, 20
    for i, (name, sub) in enumerate(cards):
        cyy = cy0 + i * (ch + gap)
        o.append(rect(kx + 20, cyy, kw - 40, ch, fill=c["panel"],
                      stroke=c["panelrule"], rx=8))
        o.append(text(kx + 44, cyy + 38, name, size=15, fill=c["ink"], weight="700"))
        o.append(text(kx + 44, cyy + 62, sub, size=12, fill=c["muted"], mono=True))

    rx0, rw = 830, 280
    cy_cloud = 128
    # OpenAI's mark is white-on-black by brand guideline, not white-on-nothing,
    # so it needs its own dark backdrop rather than the light `panel` fill
    # every other card in this figure uses. That backdrop is deliberately
    # fixed-colour, not `c["ink"]`: `ink` flips to near-white in the dark
    # palette, which would erase a white glyph exactly when the page goes dark.
    o.append(rect(rx0 + rw / 2 - 30, cy_cloud - 30, 60, 60, fill="#0b0b0f", rx=14))
    o.append(brand_logo(rx0 + rw / 2, cy_cloud, 34, LOGO_OPENAI,
                        view_w=LOGO_OPENAI_VB[0], view_h=LOGO_OPENAI_VB[1]))
    o.append(text(rx0 + rw / 2, cy_cloud + 44, "Model endpoint", size=14,
                  fill=c["ink"], anchor="middle", weight="700"))
    o.append(text(rx0 + rw / 2, cy_cloud + 64, "OpenAI-compatible, any host",
                  size=11, fill=c["muted"], anchor="middle"))

    cy_db = 300
    o.append(brand_logo(rx0 + rw / 2, cy_db, 56, LOGO_AZURE,
                        view_w=LOGO_AZURE_VB[0], view_h=LOGO_AZURE_VB[1]))
    o.append(text(rx0 + rw / 2, cy_db + 52, "Cosmos DB", size=14, fill=c["ink"],
                  anchor="middle", weight="700"))
    o.append(text(rx0 + rw / 2, cy_db + 72, "records · edges · vectors", size=11,
                  fill=c["muted"], anchor="middle"))

    cy_disk = 460
    o.append(folder_icon(rx0 + rw / 2 - 40, cy_disk - 36, 80, 60, fill=c["panel"],
                         back=c["panelrule"], stroke=c["panelrule"]))
    o.append(text(rx0 + rw / 2, cy_disk + 42, "storage/", size=14, fill=c["ink"],
                  anchor="middle", weight="700", mono=True))
    o.append(text(rx0 + rw / 2, cy_disk + 62, "PDFs · pages · extractions · app.db", size=11,
                  fill=c["muted"], anchor="middle"))

    midy = ky + kh / 2
    o.append(arrow(bx + bw, midy - 10, kx, midy - 10, stroke=c["accent"], sw=1.8))
    o.append(arrow(kx, midy + 10, bx + bw, midy + 10, stroke=c["accent"], sw=1.8))
    o.append(text((bx + bw + kx) / 2, midy - 22, "HTTPS · session token", size=11,
                  fill=c["accent"], anchor="middle", weight="600"))

    o.append(arrow(kx + kw, ky + 90, rx0, cy_cloud, stroke=c["muted"], sw=1.6))
    o.append(arrow(kx + kw, midy, rx0, cy_db, stroke=c["teal"], sw=1.6))
    o.append(arrow(kx + kw, ky + kh - 60, rx0, cy_disk, stroke=c["teal"], sw=1.6))

    return svg(W, H, "".join(o),
               title="One container, three things outside it",
               desc="A browser talks over HTTPS to a single application "
                    "container, marked with the Docker logo and holding "
                    "FastAPI, the retrieval agent and the ingestion "
                    "pipeline. The container in turn talks to an "
                    "OpenAI-compatible model endpoint, marked with the "
                    "OpenAI logo, a Cosmos DB database, marked with the "
                    "Azure logo, and a local storage folder, drawn as a "
                    "plain folder because it is a role rather than a "
                    "product.")


# --------------------------------------------------------------------------- #
# 5. workflow
# --------------------------------------------------------------------------- #
# Claim: DIVA has a short, understandable path from a source PDF to a cited
# answer. This is the orientation figure. The other figures explain the
# individual design choices in more detail.
# --------------------------------------------------------------------------- #
def workflow(c: dict) -> str:
    W, H = 1240, 350
    o: list[str] = []
    o.append(text(64, 42, "FROM DOCUMENT TO ANSWER", size=10, fill=c["accent"],
                  weight="700", spacing=1.6))
    o.append(text(64, 70, "Five steps, with the source kept beside the result.",
                  size=16, fill=c["ink"], weight="600"))

    cards = [
        ("PDF", "source file", page_icon),
        ("Read", "text + boxes", scan_icon),
        ("Fields", "named values", field_icon),
        ("Link", "families + history", graph_icon),
        ("Answer", "page citation", chat_icon),
    ]
    x0, y, cw, ch, gap = 64, 112, 204, 166, 34
    for i, (name, sub, icon_fn) in enumerate(cards):
        x = x0 + i * (cw + gap)
        o.append(rect(x, y, cw, ch, fill=c["panel"], stroke=c["panelrule"],
                      rx=10, sw=1.2))
        if icon_fn is page_icon:
            o.append(icon_fn(x + 75, y + 22, 0.82, fill=c["paper"], stroke=c["accent"]))
        elif icon_fn is scan_icon:
            o.append(icon_fn(x + 66, y + 30, 0.82, fill=c["paper"], stroke=c["ink"],
                             accent=c["accent"]))
        elif icon_fn is field_icon:
            o.append(icon_fn(x + 66, y + 32, 0.82, fill=c["paper"], stroke=c["ink"],
                             accent=c["amber"]))
        elif icon_fn is graph_icon:
            o.append(icon_fn(x + cw/2, y + 67, 0.86, fill=c["paper"], stroke=c["ink"],
                             accent=c["accent"]))
        else:
            o.append(icon_fn(x + 64, y + 34, 0.82, fill=c["paper"], stroke=c["ink"],
                             accent=c["amber"]))
        o.append(text(x + cw/2, y + 125, name, size=16, fill=c["ink"],
                      weight="700", anchor="middle"))
        o.append(text(x + cw/2, y + 148, sub, size=12, fill=c["muted"],
                      anchor="middle"))
        if i < len(cards) - 1:
            o.append(arrow(x + cw + 7, y + ch/2, x + cw + gap - 7, y + ch/2,
                           stroke=c["accent"], sw=1.8))

    o.append(text(W - 64, H - 24,
                  "OCR geometry is retained from the reader through to the citation.",
                  size=11.5, fill=c["muted"], anchor="end"))
    return svg(W, H, "".join(o),
               title="From a PDF to a cited answer",
               desc="A five-step workflow with icons: a PDF is read into text "
                    "and page boxes, named fields are extracted, document "
                    "relationships are linked, and a question is answered "
                    "with a page citation.")


# --------------------------------------------------------------------------- #
# 6. scale
# --------------------------------------------------------------------------- #
# Claim: the same per-field walk runs whether there is one chain or six, and
# a real chain found a bug the synthetic one never could.
#
# Left is the flagship demo exactly as it is everywhere else in this
# documentation: one base document and two amendments. Right is the
# real-world corpus at its actual shape, six independent chains of real
# amendment sequences, drawn small enough that the reader counts them rather
# than reads a claimed number.
# --------------------------------------------------------------------------- #
def scale(c: dict) -> str:
    W, H = 1180, 460
    o: list[str] = []

    # Left: the flagship demo, one chain of three boxes.
    lx = 64
    o.append(text(lx, 44, "FLAGSHIP DEMO", size=10, fill=c["accent"],
                  weight="700", spacing=1.6))
    o.append(text(lx, 66, "1 chain · 3 documents", size=13, fill=c["muted"]))
    names = ["base\nagreement", "first\namendment", "second\namendment"]
    bw, bh, gap = 118, 68, 46
    by = 108
    for i, label in enumerate(names):
        bx = lx + i * (bw + gap)
        o.append(rect(bx, by, bw, bh, fill=c["panel"], stroke=c["panelrule"],
                      rx=6))
        for j, ln in enumerate(label.split("\n")):
            o.append(text(bx + bw / 2, by + 30 + j * 18, ln, size=12.5,
                          fill=c["ink"], anchor="middle"))
        if i > 0:
            ax = bx - gap
            o.append(arrow(bx - 2, by + bh / 2, ax + bw + 2, by + bh / 2,
                           stroke=c["accent"], sw=1.6))
    o.append(text(lx, by + bh + 40,
                  "one base document, two amendments, both declaring what",
                  size=12.5, fill=c["muted"]))
    o.append(text(lx, by + bh + 60,
                  "they amend", size=12.5, fill=c["muted"]))

    # A vertical rule between the two panels.
    midx = 588
    o.append(line(midx, 30, midx, H - 30, stroke=c["rule"], dash="5 5"))

    # Right: the real-world corpus, six chains, drawn to their real length.
    rx0 = 636
    o.append(text(rx0, 44, "REAL-WORLD CORPUS", size=10, fill=c["accent"],
                  weight="700", spacing=1.6))
    o.append(text(rx0, 66, "6 chains · 19 documents", size=13, fill=c["muted"]))

    chains = [
        ("glu-mobile", 4), ("netgear-ingram", 3), ("federated-services", 3),
        ("pcquote-cobranding", 3), ("bellring-manufacturing", 4),
        ("neon-distributor", 2),
    ]
    sbw, sbh, sgap = 26, 20, 8
    ry0, rdy = 100, 42
    for i, (name, n) in enumerate(chains):
        ry = ry0 + i * rdy
        for j in range(n):
            sx = rx0 + j * (sbw + sgap)
            fill = c["accent"] if j == 0 else c["panel"]
            stroke = c["accent"] if j == 0 else c["panelrule"]
            o.append(rect(sx, ry, sbw, sbh, fill=fill, stroke=stroke, rx=3,
                          opacity=0.85 if j == 0 else 1))
            if j > 0:
                mx = sx - sgap
                o.append(line(sx - 1, ry + sbh / 2, mx + sbw + 1, ry + sbh / 2,
                              stroke=c["subtle"]))
        label_x = rx0 + n * (sbw + sgap) + 8
        o.append(text(label_x, ry + sbh / 2 + 4, f"{name} · {n}", size=12,
                      fill=c["muted"], mono=True))

    o.append(text(rx0, ry0 + len(chains) * rdy + 16,
                  "each chain is independent. none reference each other",
                  size=12.5, fill=c["muted"]))

    # The shared claim, spanning both panels, below everything above it.
    ruley = 386
    o.append(line(lx, ruley, W - 64, ruley, stroke=c["rule"]))
    o.append(text(lx, ruley + 26,
                  "Same walk, six independent chains instead of one. A real "
                  "chain caught a bug the synthetic one never could: a party",
                  size=12.5, fill=c["ink"]))
    o.append(text(lx, ruley + 46,
                  "name with a slash in it broke the graph build for every "
                  "document processed after it.", size=12.5, fill=c["ink"]))

    return svg(W, H, "".join(o),
               title="The same mechanism, run at nineteen times the scale",
               desc="Two panels side by side. On the left, the flagship "
                    "demo's single chain of three documents. On the right, "
                    "the real-world corpus's six independent chains totalling "
                    "nineteen documents, each drawn to its real length. A "
                    "caption notes that a real amendment chain caught a bug "
                    "the synthetic demo never could.")


FIGURES = {
    "evidence-chain": evidence_chain,
    "domain-layers": domain_layers,
    "retrieval-modes": retrieval_modes,
    "architecture": architecture,
    "workflow": workflow,
    "scale": scale,
}


def main() -> int:
    for name, fn in FIGURES.items():
        for theme, palette in (("light", LIGHT), ("dark", DARK)):
            path = OUT / f"{name}-{theme}.svg"
            path.write_text(fn(palette), encoding="utf-8", newline="\n")
            print(f"  {path.relative_to(OUT.parents[1])}  "
                  f"{path.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
