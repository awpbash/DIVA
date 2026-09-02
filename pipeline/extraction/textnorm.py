r"""textnorm.py: shared reader-artifact normalization for snippet matching.

The CU reader (cu_read.py) writes page text as markdown. Tables arrive
as pipe rows with alignment separators, or as HTML table scaffolding,
plus backslash escapes and HTML entities:

    | Rate for Demand Charge | S$1,576,035 |
    | --- | --- |
    <table><tr><td>Contract Year</td><td>Minimum Usage</td></tr></table>
    <!-- PageNumber: 35 -->

An LLM-quoted evidence snippet reproduces the WORDS but not the table
syntax, so a verbatim re-find of a genuine quote against the page
markdown fails on rendering artifacts alone. RapidOCR pages carried none
of this, which is why the verbatim-match rate dropped when the reader
switched.

``neutralize_markdown`` strips ONLY rendering artifacts. It must be
applied to BOTH sides of a comparison (the snippet and the page text),
so matching stays exact-after-normalization: a snippet whose words are
not on the page still fails. It never rewrites stored snippets, rects or
page files. It exists purely inside match comparisons.

What it neutralizes:

* markdown table syntax: separator rows (``| --- |:---:|``) dropped,
  pipe cell borders (inner, leading, trailing) collapsed to a space
* HTML scaffolding and comments CU embeds in markdown (``<table>``,
  ``<tr>``, ``<td>``, ``<br>``, ``<!-- PageNumber -->``), each collapsed
  to a space via a closed tag whitelist so literal text like "<2500"
  survives
* markdown backslash escapes unescaped (``\|`` ``\_`` ``\*`` and kin)
* HTML entities decoded (``&amp;`` becomes ``&``)
* conservative unicode folding: dash variants to "-", curly quotes to
  straight quotes, non-breaking and thin spaces to plain space, soft
  hyphens dropped, degree-sign ligatures expanded. Deliberately NOT full
  NFKC: folding superscripts would inject phantom digits (m3 out of m³)
  into the scorer's number extraction
* whitespace collapsed to single spaces

No lowercasing, no punctuation stripping, no fuzzing. Callers layer
their own case handling and tolerance on top.
"""
from __future__ import annotations

import html
import re

# A markdown table separator row: nothing but pipes, colons, dashes and
# whitespace, with at least one run of two or more dashes. Matches
# "| --- | --- |", "|:---:|----|" and bare "---|---". A prose line can
# never match because any letter or digit breaks the character class.
_SEPARATOR_ROW = re.compile(r"^[\s|:-]*-{2,}[\s|:-]*$")

# HTML scaffolding CU emits inside markdown. A closed whitelist, never a
# generic <...> pattern, so a literal "<2500" in page text is not eaten.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_HTML_TAG = re.compile(
    r"</?(?:table|thead|tbody|tfoot|tr|td|th|caption|br|hr|p|div|span|"
    r"b|i|u|em|strong|sub|sup)\b[^>]*/?>",
    re.I,
)

# Markdown backslash escapes for literal punctuation.
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~<>])")

# Conservative fold table: the dash family to "-", the curly quote
# families to straight quotes, exotic spaces to plain space, soft hyphen
# dropped, degree ligatures expanded. An explicit table, not NFKC, so
# superscript digits and other compatibility forms stay untouched.
_CHAR_FOLDS = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    " ": " ", " ": " ", " ": " ", " ": " ",
    "­": "",
    "℃": "°C", "℉": "°F",
})

_WS = re.compile(r"\s+")


def neutralize_markdown(s: str) -> str:
    """Return ``s`` with reader/markdown rendering artifacts removed.

    Apply to BOTH sides of a snippet-vs-page comparison. Content words,
    numbers, casing and real punctuation survive untouched, so matching
    after this stays exact: only table syntax and encoding noise is
    equalised away.
    """
    if not s:
        return ""
    # Tags and comments before entity decode: an escaped literal
    # "&lt;table&gt;" in page text must NOT decode into a strippable tag.
    s = _HTML_COMMENT.sub(" ", s)
    s = _HTML_TAG.sub(" ", s)
    s = html.unescape(s)
    s = s.translate(_CHAR_FOLDS)
    # Separator rows are identified per line, before pipes are collapsed.
    s = "\n".join(ln for ln in s.split("\n") if not _SEPARATOR_ROW.match(ln))
    s = _MD_ESCAPE.sub(r"\1", s)
    s = s.replace("|", " ")
    return _WS.sub(" ", s).strip()
