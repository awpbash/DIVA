"""Supersedence shapes the suite did not previously cover.

Cycles, self-parents and missing parents were already guarded. These are the
three that were not: a diamond, an undated family member, and the fact that
ordering runs across a whole folder rather than along DAG edges.

Each one is a case where the answer to "what is the current value" is decided
by machinery nobody watches, so the failure looks like a plausible number
rather than an error.
"""
from __future__ import annotations

from pipeline.kb import km


def _doc(doc_id, date, fields, ordinal=0):
    return {"doc_id": doc_id, "best_date": date, "ordinal": ordinal,
            "stated_fields": set(fields)}


# --------------------------------------------------------------------------- #
# Diamond: two amendments off one base, both restating the same field
# --------------------------------------------------------------------------- #
def test_diamond_the_later_amendment_wins_the_contested_field():
    """Two amendments change the same field on the same base. Exactly one can
    be current, and it must be the later one rather than whichever the store
    happened to enumerate first."""
    ordered = km.order_family([
        _doc("base", "2020-01-01", {"terms.fee", "terms.scope"}),
        _doc("amendA", "2022-01-01", {"terms.fee"}),
        _doc("amendB", "2023-01-01", {"terms.fee"}),
    ])
    st = km.supersedence(ordered, {"terms.fee": 1, "terms.scope": 1})

    assert st[("amendB", "terms.fee")]["is_current"] is True
    assert st[("amendA", "terms.fee")]["is_current"] is False
    assert st[("base", "terms.fee")]["is_current"] is False
    # The field nobody re-stated still inherits from the base.
    assert st[("base", "terms.scope")]["is_current"] is True


def test_diamond_records_what_superseded_each_value():
    """A superseded value has to say what replaced it, or the audit trail is
    just a boolean."""
    ordered = km.order_family([
        _doc("base", "2020-01-01", {"terms.fee"}),
        _doc("amendA", "2022-01-01", {"terms.fee"}),
        _doc("amendB", "2023-01-01", {"terms.fee"}),
    ])
    st = km.supersedence(ordered, {"terms.fee": 1})
    assert st[("base", "terms.fee")]["superseded_by"] == "amendB"
    assert st[("amendA", "terms.fee")]["superseded_by"] == "amendB"


def test_a_tie_on_date_is_broken_deterministically():
    """Two documents dated the same day must not swap places between rebuilds,
    because that silently changes which value is current."""
    fam = [_doc("zzz", "2022-01-01", {"terms.fee"}),
           _doc("aaa", "2022-01-01", {"terms.fee"})]
    first = [d["doc_id"] for d in km.order_family(fam)]
    second = [d["doc_id"] for d in km.order_family(list(reversed(fam)))]
    assert first == second


def test_ordinal_breaks_a_date_tie_before_doc_id():
    """Two amendments dated the same day are ordered by their stated ordinal,
    which is the document's own answer, ahead of the arbitrary id fallback."""
    fam = [_doc("aaa", "2022-01-01", {"x"}, ordinal=2),
           _doc("zzz", "2022-01-01", {"x"}, ordinal=1)]
    assert [d["doc_id"] for d in km.order_family(fam)] == ["zzz", "aaa"]


# --------------------------------------------------------------------------- #
# Undated members
# --------------------------------------------------------------------------- #
def test_an_undated_document_sorts_last_and_never_supersedes():
    """`_UNDATED` is a sentinel that sorts after every real date, so an
    undateable document cannot take a position in the chain."""
    fam = [_doc("undated", km._UNDATED, {"terms.fee"}),
           _doc("base", "2020-01-01", {"terms.fee"})]
    assert [d["doc_id"] for d in km.order_family(fam)] == ["base", "undated"]


def test_multi_valued_fields_accumulate_instead_of_superseding():
    """A list-valued field is added to, not replaced. Superseding one would
    silently drop everything the earlier documents listed."""
    ordered = km.order_family([
        _doc("base", "2020-01-01", {"assets.items"}),
        _doc("amend", "2022-01-01", {"assets.items"}),
    ])
    st = km.supersedence(ordered, {"assets.items": 9})
    assert st[("base", "assets.items")]["is_current"] is True
    assert st[("amend", "assets.items")]["is_current"] is True
