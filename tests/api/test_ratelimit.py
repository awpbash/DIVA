"""api/ratelimit.py: the in-memory sliding-window limiter behind /auth/login
and /chat. Pure unit tests against the module functions directly, no app,
no network."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from api import ratelimit


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test starts from an empty counter table, matching what a real
    process restart (or ratelimit.reset() at api/main.py's lifespan) gives
    for free."""
    ratelimit.reset()
    yield
    ratelimit.reset()


# --------------------------------------------------------------------------- #
# allow(), sliding window
# --------------------------------------------------------------------------- #
def test_allows_up_to_the_limit_then_blocks():
    for _ in range(3):
        assert ratelimit.allow("k", limit=3, window=60.0) is True
    assert ratelimit.allow("k", limit=3, window=60.0) is False


def test_a_rejected_call_does_not_buy_itself_more_rope():
    # A rejected attempt must not be counted, or repeated rejected calls
    # would eventually stack up and let a later legitimate call through.
    for _ in range(3):
        ratelimit.allow("k", limit=3, window=60.0)
    for _ in range(5):
        assert ratelimit.allow("k", limit=3, window=60.0) is False
    assert len(ratelimit._HITS["k"]) == 3


def test_reset_clears_standing_counts():
    for _ in range(3):
        ratelimit.allow("k", limit=3, window=60.0)
    assert ratelimit.allow("k", limit=3, window=60.0) is False
    ratelimit.reset()
    assert ratelimit.allow("k", limit=3, window=60.0) is True


def test_different_keys_do_not_share_a_counter():
    for _ in range(3):
        ratelimit.allow("a", limit=3, window=60.0)
    assert ratelimit.allow("a", limit=3, window=60.0) is False
    assert ratelimit.allow("b", limit=3, window=60.0) is True


def test_hits_older_than_the_window_fall_out_of_it():
    """A short window lets the oldest hit expire within the test's own
    runtime, exercising the same trailing-window eviction a real 60s
    window relies on."""
    assert ratelimit.allow("k", limit=1, window=0.05) is True
    assert ratelimit.allow("k", limit=1, window=0.05) is False
    time.sleep(0.08)
    assert ratelimit.allow("k", limit=1, window=0.05) is True


# --------------------------------------------------------------------------- #
# client_key(), caller identity for throttling
# --------------------------------------------------------------------------- #
def _request(headers: dict, client_host: str | None):
    client = SimpleNamespace(host=client_host) if client_host is not None else None
    return SimpleNamespace(headers=headers, client=client)


def test_client_key_prefers_x_forwarded_for():
    req = _request({"x-forwarded-for": "203.0.113.5, 10.0.0.1"}, "10.0.0.1")
    assert ratelimit.client_key(req) == "203.0.113.5"


def test_client_key_takes_only_the_first_hop():
    req = _request({"x-forwarded-for": " 203.0.113.5 , 198.51.100.9"}, "10.0.0.1")
    assert ratelimit.client_key(req) == "203.0.113.5"


def test_client_key_falls_back_to_the_socket_address():
    req = _request({}, "192.168.1.7")
    assert ratelimit.client_key(req) == "192.168.1.7"


def test_client_key_with_no_client_and_no_header():
    req = _request({}, None)
    assert ratelimit.client_key(req) == "unknown"
