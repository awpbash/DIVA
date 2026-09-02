"""Unit tests for the hybrid-retrieval primitives: keyword tokenization +
scoring (`keyword_search`'s ranking, the Lucene index's replacement in the
Cosmos port) and Reciprocal Rank Fusion (the agent's bundle ordering). Both
pure — no DB, no network.
"""
from __future__ import annotations

from api.rag.agent import _rrf_scores
from pipeline.store.query import keyword_score, tokenize


# --- keyword tokenization / scoring -----------------------------------------

def test_tokenize_keeps_alphanumeric_spec_tokens():
    # The whole point of the lexical arm: "1600a" / "200" stay intact for
    # exact matching (lowercased, but character-exact).
    toks = tokenize("1600A changeover switch")
    assert "1600a" in toks and "changeover" in toks
    toks = tokenize("200 RT magnetic bearing")
    assert "200" in toks and "magnetic" in toks


def test_tokenize_drops_stopwords_keeps_clause_numbers():
    toks = tokenize("what is clause 9.3 (b): the changeover?")
    assert "the" not in toks and "is" not in toks
    assert "changeover" in toks
    assert "9.3" in toks


def test_tokenize_empty_when_nothing_searchable():
    assert tokenize("") == []
    assert tokenize("   ") == []


def test_keyword_score_ranks_matching_text_higher():
    q = tokenize("1600A changeover switch")
    hit = keyword_score(q, "the 1600A changeover switch shall be maintained")
    near = keyword_score(q, "a changeover procedure applies")
    miss = keyword_score(q, "system supply temperature")
    assert hit > near > miss == 0.0


# --- RRF fusion ------------------------------------------------------------

def test_rrf_rewards_cross_list_agreement():
    # 'a' is rank 1 in BOTH lists; 'b' is rank 1 in only one. 'a' must win.
    dense = ["b", "a", "c"]
    bm25 = ["a", "d"]
    s = _rrf_scores([dense, bm25])
    assert s["a"] > s["b"]
    assert s["a"] > s["d"]


def test_rrf_single_list_orders_by_rank():
    s = _rrf_scores([["x", "y", "z"]])
    assert s["x"] > s["y"] > s["z"]


def test_rrf_k_damps_and_is_one_based():
    # Top hit of a single list contributes exactly 1/(k+1).
    s = _rrf_scores([["only"]], k=60)
    assert abs(s["only"] - 1.0 / 61) < 1e-9


def test_rrf_ignores_empty_ids_and_lists():
    s = _rrf_scores([[], ["", "real"]])
    assert "" not in s
    assert "real" in s
