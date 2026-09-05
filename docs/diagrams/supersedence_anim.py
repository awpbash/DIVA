"""supersedence_anim.py — the animated figure for per-field supersedence.

    py -3.14 -m manim render -qh docs/diagrams/supersedence_anim.py Supersedence

Then convert the MP4 to a palette-optimised GIF (see docs/diagrams/README.md).
Manim is NOT a project dependency. It renders one figure that is committed as
an artifact, so nobody needs it installed to read the documentation.

Claim, and the only reason this figure is animated: "current" is resolved one
field at a time, by walking backwards along the amendment chain until a
document sets that field. A still image can show the end state. It cannot show
the walk skipping a document that says nothing, which is the part people get
wrong when they assume the newest document wins outright.

Colours track the application's dark theme (web/src/styles.css).
"""
from __future__ import annotations

from manim import (BOLD, DOWN, RIGHT, UP, Create, DashedLine, FadeIn, FadeOut,
                   Line, Rectangle, Scene, Text, VGroup, Write, config)

INK = "#ECEDF1"
MUTED = "#9DA3AE"
SUBTLE = "#6C7280"
RULE = "#33353D"
CARD = "#1C1D23"
ACCENT = "#76A3FF"
AMBER = "#F0B429"
TEAL = "#4DD4AC"

SANS = "Segoe UI"
MONO = "Consolas"

config.background_color = "#16171C"

# Column centres. Left to right is time, carried by the dates on the cards
# rather than by an axis line: an axis drawn through the row of cards reads as
# a strike-through, and the dates already say it.
COLS = [-3.1, 0.15, 3.4]
CURRENT_X = 5.9
ROWS = [0.75, -0.75]
LABEL_X = -6.6
CARD_Y = 2.55
DIVIDER_X = 4.85


def label(s: str, size: float, colour: str, *, mono: bool = False,
          weight: str = "NORMAL") -> Text:
    return Text(s, font=MONO if mono else SANS, font_size=size, color=colour,
                weight=weight)


class Supersedence(Scene):
    def construct(self) -> None:
        self.build_chain()
        self.build_grid()
        self.fill_cells()
        self.explain_blank()
        self.walk(row=0, stops=[2, 1], lands=1, answer="SGD 61,500",
                  note="First Amendment")
        self.walk(row=1, stops=[2], lands=2, answer="5 years",
                  note="Second Amendment")
        self.close()

    # -- the document family, read from what the documents declare ---------- #
    def build_chain(self) -> None:
        docs = [("Base Agreement", "14 Mar 2023"),
                ("First Amendment", "2 Sep 2024"),
                ("Second Amendment", "20 Jan 2025")]
        self.cards = VGroup()
        for (title, date), x in zip(docs, COLS):
            box = Rectangle(width=2.5, height=0.95, stroke_color=RULE,
                            stroke_width=1.5, fill_color=CARD, fill_opacity=1)
            # "Second Amendment" is wider than the box at this font size and
            # used to run past both edges, so shrink the title to fit instead
            # of hand-tuning a size that only happens to work for these three
            # strings.
            t = label(title, 21, INK, weight=BOLD)
            max_w = box.width - 0.3
            if t.width > max_w:
                t.scale_to_fit_width(max_w)
            t.move_to(box).shift(UP * 0.17)
            d = label(date, 17, MUTED, mono=True).move_to(box).shift(DOWN * 0.2)
            card = VGroup(box, t, d).move_to([x, CARD_Y, 0])
            self.cards.add(card)
        self.play(FadeIn(self.cards, shift=DOWN * 0.15, lag_ratio=0.25),
                  run_time=1.1)

        # No arrowheads: AMENDS points backwards in time while the row reads
        # forwards, so a head would be pointing the wrong way whichever end it
        # went on. The label carries the meaning instead.
        links = VGroup()
        for a, b in ((0, 1), (1, 2)):
            links.add(Line([COLS[a] + 1.29, CARD_Y, 0],
                           [COLS[b] - 1.29, CARD_Y, 0],
                           stroke_color=ACCENT, stroke_width=2.5))
        note = label("AMENDS  ·  declared in the recitals, not inferred",
                     19, ACCENT).move_to([0.15, 1.72, 0])
        self.play(Create(links), FadeIn(note), run_time=0.9)
        self.wait(0.8)
        self.play(FadeOut(note), run_time=0.4)

    # -- two fields, tracked across the family ------------------------------ #
    def build_grid(self) -> None:
        self.row_labels = VGroup()
        for name, y in zip(("Licence fee", "Initial term"), ROWS):
            t = label(name, 23, INK).move_to([LABEL_X, y, 0])
            t.shift(RIGHT * (t.width / 2 - 0.05))
            self.row_labels.add(t)
        divider = DashedLine([DIVIDER_X, 1.35, 0], [DIVIDER_X, -1.6, 0],
                             stroke_color=RULE, stroke_width=1.5,
                             dash_length=0.09)
        header = label("CURRENT", 18, TEAL, weight=BOLD)
        header.move_to([CURRENT_X, 1.75, 0])
        derived = label("resolved, not stored", 16, SUBTLE)
        derived.move_to([CURRENT_X, 1.38, 0])
        self.play(FadeIn(self.row_labels, lag_ratio=0.3),
                  Create(divider), FadeIn(header), FadeIn(derived),
                  run_time=0.9)

    # -- what each document actually says ----------------------------------- #
    def fill_cells(self) -> None:
        # None means the document is silent on that field.
        table = [["SGD 48,000", "SGD 61,500", None],
                 ["3 years", None, "5 years"]]
        self.cells: list[list[Text]] = []
        anims = []
        for r, row in enumerate(table):
            made = []
            for cidx, value in enumerate(row):
                if value is None:
                    t = label("—", 26, SUBTLE)
                else:
                    t = label(value, 24, INK, mono=True)
                t.move_to([COLS[cidx], ROWS[r], 0])
                made.append(t)
                anims.append(FadeIn(t, shift=UP * 0.1))
            self.cells.append(made)
        self.play(*anims, lag_ratio=0.12, run_time=1.4)
        self.wait(0.4)

    # -- the rule that makes a blank meaningful ----------------------------- #
    def explain_blank(self) -> None:
        # Stays on screen through both walks. It is the rule being applied, so
        # taking it away while the walk applies it would be perverse.
        self.rule_note = label(
            "a blank in an amendment means unchanged, not missing",
            22, AMBER).move_to([0, -2.5, 0])
        self.play(Write(self.rule_note), run_time=1.0)
        self.wait(1.0)

    # -- the walk ----------------------------------------------------------- #
    def walk(self, *, row: int, stops: list[int], lands: int, answer: str,
             note: str) -> None:
        y = ROWS[row]
        band = Rectangle(width=13.6, height=0.86, stroke_width=0,
                         fill_color=ACCENT, fill_opacity=0.07)
        band.move_to([0.05, y, 0])
        self.play(FadeIn(band), run_time=0.3)

        caret = label("◀", 22, ACCENT).move_to([4.45, y, 0])
        self.play(FadeIn(caret), run_time=0.3)

        for cidx in stops:
            self.play(caret.animate.move_to([COLS[cidx] + 1.35, y, 0]),
                      run_time=0.55)
            if cidx != lands:
                skip = label("silent, keep walking", 18, SUBTLE)
                skip.move_to([COLS[cidx], y - 0.62, 0])
                self.play(FadeIn(skip), run_time=0.35)
                self.wait(0.5)
                self.play(FadeOut(skip), run_time=0.3)

        hit = Rectangle(width=2.1, height=0.62, stroke_color=TEAL,
                        stroke_width=2, fill_opacity=0)
        hit.move_to([COLS[lands], y, 0])
        self.play(Create(hit), run_time=0.45)

        answer_t = label(answer, 24, TEAL, mono=True)
        answer_t.move_to([COLS[lands], y, 0])
        self.add(answer_t)
        source = label(note, 16, SUBTLE).move_to([CURRENT_X, y - 0.46, 0])
        self.play(answer_t.animate.move_to([CURRENT_X, y, 0]), run_time=0.9)
        self.play(FadeIn(source), FadeOut(caret), FadeOut(band), run_time=0.4)
        self.wait(0.5)
        self.play(FadeOut(hit), run_time=0.3)

    def close(self) -> None:
        punch = label("“current” is resolved per field, not per document",
                      27, INK, weight=BOLD).move_to([0, -2.5, 0])
        self.play(FadeOut(self.rule_note), run_time=0.35)
        self.play(Write(punch), run_time=1.2)
        self.wait(2.4)
