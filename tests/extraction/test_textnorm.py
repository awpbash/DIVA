"""Shared reader-artifact normalization (pipeline/extraction/textnorm.py).

The contract under test: the CU reader renders page text as markdown
(pipe tables, alignment separator rows, HTML table scaffolding, escapes,
entities), so an LLM-quoted snippet that is genuinely on the page stops
matching the raw page text verbatim. ``neutralize_markdown`` is applied
to BOTH sides of the comparison in the validate stage and the scorer,
and must

* rescue genuine quotes buried in table syntax,
* still fail a snippet whose words are not on the cited page (matching
  stays exact-after-normalization, never fuzzy or substring-similar),
* leave stored snippets untouched (callers keep the raw strings).

Page fixtures mirror real CU output in storage/pages_md (both the pipe
table and the HTML table shapes CU emits).
"""
from __future__ import annotations

from types import SimpleNamespace


from eval.extraction.score import _match, _norm, _nums, _present
from pipeline.extraction.textnorm import neutralize_markdown


# ---------------------------------------------------------------------------
# neutralize_markdown, pure behaviour
# ---------------------------------------------------------------------------


def test_pipe_borders_collapse():
    assert (neutralize_markdown("| Rate for Demand Charge | S$1,576,035 |")
            == "Rate for Demand Charge S$1,576,035")


def test_separator_rows_dropped():
    assert neutralize_markdown("| A | B |\n| --- | --- |\n| 1 | 2 |") == "A B 1 2"
    assert neutralize_markdown("|:---:|----|") == ""
    assert neutralize_markdown("| --- |") == ""


def test_prose_with_dashes_is_not_a_separator_row():
    s = "clause 9.5 -- see Schedule 3"
    assert neutralize_markdown(s) == s


def test_html_scaffolding_stripped():
    s = "<table><tr><td>Contract Year</td><td>1</td></tr></table>"
    assert neutralize_markdown(s) == "Contract Year 1"


def test_html_comments_dropped():
    s = "x <!-- PageNumber: 35 -->\n<!-- PageBreak --> y"
    assert neutralize_markdown(s) == "x y"


def test_br_becomes_space():
    assert (neutralize_markdown("Attention :<br>Assistant General Manager")
            == "Attention : Assistant General Manager")


def test_literal_comparisons_survive_tag_stripping():
    # The tag whitelist must never eat real page text like "<2500".
    assert (neutralize_markdown("Chilled water 20°C: <2500")
            == "Chilled water 20°C: <2500")


def test_markdown_escapes_unescaped():
    assert (neutralize_markdown(r"AHU\_01 rated 7\.5 kW \*derated\*")
            == "AHU_01 rated 7.5 kW *derated*")


def test_escaped_pipe_and_raw_pipe_neutralize_identically():
    # A literal pipe on the page arrives escaped in CU markdown but raw in
    # the LLM quote. Both sides must land on the same neutral form.
    assert (neutralize_markdown(r"Zone A \| Zone B")
            == neutralize_markdown("Zone A | Zone B")
            == "Zone A Zone B")


def test_html_entities_decoded_after_tag_stripping():
    # &lt; decodes to a literal < and must NOT then be treated as a tag.
    assert neutralize_markdown("M&amp;E works &lt;100 mm") == "M&E works <100 mm"


def test_unicode_folds():
    assert (neutralize_markdown("Supplier’s Equipment – 7.0 °C")
            == "Supplier's Equipment - 7.0 °C")


def test_soft_hyphen_dropped():
    assert neutralize_markdown("tem­perature") == "temperature"


def test_whitespace_collapsed_and_empty():
    assert neutralize_markdown("a\n\n  b\tc") == "a b c"
    assert neutralize_markdown("") == ""


def test_plain_prose_is_untouched():
    s = "The Consumption Charge rate is S$0.4576/RTh."
    assert neutralize_markdown(s) == s


# ---------------------------------------------------------------------------
# Scorer comparisons, markdown-neutral on both sides
# ---------------------------------------------------------------------------


def test_scorer_norm_is_markdown_neutral():
    assert _norm("| CHW | 0.58 |") == "chw 0.58"
    assert _norm("<td>580,000 RTh</td>") == "580,000 rth"


def test_scorer_present_rejects_pure_table_syntax():
    assert _present("| --- |") is False
    assert _present("580,000 RTh") is True


def test_scorer_nums_reads_through_table_markup():
    assert _nums("| 1 | 580,000 RTh |") == [1.0, 580000.0]


def test_scorer_match_value_through_table_markup():
    f = SimpleNamespace(type="value")
    assert _match(f, "580,000 RTh", ["<td>580,000 RTh</td>"])
    assert _match(f, ["580,000 RTh", "700,000 RTh"],
                  ["| 1 | 580,000 RTh |", "| 2 | 700,000 RTh |"])
    assert not _match(f, "750,000 RTh", ["| 580,000 RTh |"])


def test_scorer_match_text_through_table_markup():
    f = SimpleNamespace(type="text")
    assert _match(f, "Contract Year", ["<tr><td>Contract Year</td></tr>"])
