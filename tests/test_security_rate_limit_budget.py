"""Rejected requests must not consume rate-limit budget.

The limiter used to increment before comparing, so ``count`` tallied *attempts*
rather than *admissions* — 10 requests against a limit of 5 left ``count == 10``.

Accept/reject behaviour was unaffected, because this is a fixed window that
resets wholesale and the inflated value was never read back. These tests pin the
invariant anyway, for the reason the fix was made: the moment this becomes a
sliding window, or the moment anything derives a ``Retry-After`` or a metric
from ``count``, a counter that includes rejections starves a well-behaved client
that is backing off correctly. That failure would be silent and would look like
a client bug.
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _isolate_rate_counters():
    from palinode.api import server

    saved = server._rate_counters.copy()
    server._rate_counters.clear()
    yield server
    server._rate_counters.clear()
    server._rate_counters.update(saved)


def test_counter_tracks_admissions_not_attempts(_isolate_rate_counters) -> None:
    s = _isolate_rate_counters
    limit = 5
    results = [s._check_rate_limit("10.0.0.1", "write", limit) for _ in range(10)]

    assert results.count(True) == limit, "admitted count must equal the limit"
    assert results[:limit] == [True] * limit, "the first N must be the admitted ones"
    assert results[limit:] == [False] * limit

    entry = s._rate_counters["10.0.0.1:write"]
    assert entry["count"] == limit, (
        f"count={entry['count']} but only {limit} requests were admitted — "
        "rejected requests are consuming budget"
    )


def test_sustained_overload_does_not_inflate_counter(_isolate_rate_counters) -> None:
    """A client hammering far over the limit must not drive the counter up.

    This is the case that would starve a backing-off client under a sliding
    window: every rejected attempt topping up the counter it is waiting to
    drain.
    """
    s = _isolate_rate_counters
    limit = 3
    for _ in range(200):
        s._check_rate_limit("10.0.0.2", "write", limit)

    entry = s._rate_counters["10.0.0.2:write"]
    assert entry["count"] <= limit, (
        f"count={entry['count']} after 200 attempts against a limit of {limit}"
    )


def test_backing_off_client_is_admitted_after_the_window(
    _isolate_rate_counters, monkeypatch
) -> None:
    """The behaviour a well-behaved client depends on, pinned explicitly."""
    s = _isolate_rate_counters
    # Patch the DEFINING module, not `server`'s re-export. `_check_rate_limit`
    # lives in palinode.api.rate_limit and reads its own module global, so
    # setting server._RATE_LIMIT_WINDOW rebinds an alias the function never
    # consults and the test silently exercises the real 60s window.
    from palinode.api import rate_limit as rl

    monkeypatch.setattr(rl, "_RATE_LIMIT_WINDOW", 0.3)
    limit = 2

    assert s._check_rate_limit("10.0.0.3", "write", limit) is True
    assert s._check_rate_limit("10.0.0.3", "write", limit) is True
    assert s._check_rate_limit("10.0.0.3", "write", limit) is False

    time.sleep(0.35)
    assert s._check_rate_limit("10.0.0.3", "write", limit) is True, (
        "a client that waits out the window must be admitted"
    )


def test_limit_is_still_enforced(_isolate_rate_counters) -> None:
    """Guard against 'fixing' the counter by letting everything through."""
    s = _isolate_rate_counters
    admitted = sum(
        1 for _ in range(50) if s._check_rate_limit("10.0.0.4", "search", 7)
    )
    assert admitted == 7
